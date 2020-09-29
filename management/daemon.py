import os, os.path, re, json, time
import multiprocessing.pool, subprocess

from functools import wraps

from flask import Flask, request, render_template, abort, Response, send_from_directory, make_response

import auth, utils, mfa
from mailconfig import get_mail_users, get_mail_users_ex, get_admins, add_mail_user, set_mail_password, set_mail_display_name, remove_mail_user
from mailconfig import get_mail_user_privileges, add_remove_mail_user_privilege
from mailconfig import get_mail_aliases, get_mail_aliases_ex, get_mail_domains, add_mail_alias, remove_mail_alias
from mfa import get_mfa_state, enable_mfa, disable_mfa
import mfa_totp

env = utils.load_environment()

auth_service = auth.KeyAuthService()

# We may deploy via a symbolic link, which confuses flask's template finding.
me = __file__
try:
	me = os.readlink(__file__)
except OSError:
	pass

# for generating CSRs we need a list of country codes
csr_country_codes = []
with open(os.path.join(os.path.dirname(me), "csr_country_codes.tsv")) as f:
	for line in f:
		if line.strip() == "" or line.startswith("#"): continue
		code, name = line.strip().split("\t")[0:2]
		csr_country_codes.append((code, name))

app = Flask(__name__, template_folder=os.path.abspath(os.path.join(os.path.dirname(me), "templates")))

# Decorator to protect views that require a user with 'admin' privileges.
def authorized_personnel_only(viewfunc):
	@wraps(viewfunc)
	def newview(*args, **kwargs):
		# Authenticate the passed credentials, which is either the API key or a username:password pair
		# and an optional X-Auth-Token token.
		error = None
		privs = []

		try:
			email, privs = auth_service.authenticate(request, env)
		except ValueError as e:
			# Write a line in the log recording the failed login
			log_failed_login(request)

			# Authentication failed.
			error = str(e)

		# Authorized to access an API view?
		if "admin" in privs:
			# Store the email address of the logged in user so it can be accessed
			# from the API methods that affect the calling user.
			request.user_email = email
			request.user_privs = privs

			# Call view func.
			return viewfunc(*args, **kwargs)

		if not error:
			error = "You are not an administrator."

		# Not authorized. Return a 401 (send auth) and a prompt to authorize by default.
		status = 401
		headers = {
			'WWW-Authenticate': 'Basic realm="{0}"'.format(auth_service.auth_realm),
			'X-Reason': error,
		}

		if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
			# Don't issue a 401 to an AJAX request because the user will
			# be prompted for credentials, which is not helpful.
			status = 403
			headers = None

		if request.headers.get('Accept') in (None, "", "*/*"):
			# Return plain text output.
			return Response(error+"\n", status=status, mimetype='text/plain', headers=headers)
		else:
			# Return JSON output.
			return Response(json.dumps({
				"status": "error",
				"reason": error,
				})+"\n", status=status, mimetype='application/json', headers=headers)

	return newview

@app.errorhandler(401)
def unauthorized(error):
	return auth_service.make_unauthorized_response()

def json_response(data, status=200):
	return Response(json.dumps(data, indent=2, sort_keys=True)+'\n', status=status, mimetype='application/json')

###################################

# Control Panel (unauthenticated views)

@app.route('/')
def index():
	# Render the control panel. This route does not require user authentication
	# so it must be safe!

	no_users_exist = (len(get_mail_users(env)) == 0)
	no_admins_exist = (len(get_admins(env)) == 0)

	utils.fix_boto() # must call prior to importing boto
	import boto.s3
	backup_s3_hosts = [(r.name, r.endpoint) for r in boto.s3.regions()]

	return render_template('index.html',
		hostname=env['PRIMARY_HOSTNAME'],
		storage_root=env['STORAGE_ROOT'],

		no_users_exist=no_users_exist,
		no_admins_exist=no_admins_exist,

		backup_s3_hosts=backup_s3_hosts,
		csr_country_codes=csr_country_codes,
	)

@app.route('/me')
def me():
	# Is the caller authorized?
	try:
		email, privs = auth_service.authenticate(request, env)
	except ValueError as e:
		if "missing-totp-token" in str(e):
			return json_response({
				"status": "missing-totp-token",
				"reason": str(e),
			})
		else:
			# Log the failed login
			log_failed_login(request)
			return json_response({
				"status": "invalid",
				"reason": str(e),
			})

	resp = {
		"status": "ok",
		"email": email,
		"privileges": privs,
	}

	# Is authorized as admin? Return an API key for future use.
	if "admin" in privs:
		resp["api_key"] = auth_service.create_user_key(email, env)

	# Return.
	return json_response(resp)

# MAIL

@app.route('/mail/users')
@authorized_personnel_only
def mail_users():
	if request.args.get("format", "") == "json":
		return json_response(get_mail_users_ex(env, with_archived=True))
	else:
		return "".join(x+"\n" for x in get_mail_users(env))

@app.route('/mail/users/add', methods=['POST'])
@authorized_personnel_only
def mail_users_add():
	try:
		return add_mail_user(request.form.get('email', ''), request.form.get('password', ''), request.form.get('privileges', ''), request.form.get('display_name', ''), env)
	except ValueError as e:
		return (str(e), 400)

@app.route('/mail/users/password', methods=['POST'])
@authorized_personnel_only
def mail_users_password():
	try:
		return set_mail_password(request.form.get('email', ''), request.form.get('password', ''), env)
	except ValueError as e:
		return (str(e), 400)

@app.route('/mail/users/display_name', methods=['POST'])
@authorized_personnel_only
def mail_users_display_name():
	try:
		return set_mail_display_name(request.form.get('email', ''), request.form.get('display_name', ''), env)
	except ValueError as e:
		return (str(e), 400)

@app.route('/mail/users/remove', methods=['POST'])
@authorized_personnel_only
def mail_users_remove():
	return remove_mail_user(request.form.get('email', ''), env)


@app.route('/mail/users/privileges')
@authorized_personnel_only
def mail_user_privs():
	privs = get_mail_user_privileges(request.args.get('email', ''), env)
	if isinstance(privs, tuple): return privs # error
	return "\n".join(privs)

@app.route('/mail/users/privileges/add', methods=['POST'])
@authorized_personnel_only
def mail_user_privs_add():
	return add_remove_mail_user_privilege(request.form.get('email', ''), request.form.get('privilege', ''), "add", env)

@app.route('/mail/users/privileges/remove', methods=['POST'])
@authorized_personnel_only
def mail_user_privs_remove():
	return add_remove_mail_user_privilege(request.form.get('email', ''), request.form.get('privilege', ''), "remove", env)


@app.route('/mail/aliases')
@authorized_personnel_only
def mail_aliases():
	if request.args.get("format", "") == "json":
		return json_response(get_mail_aliases_ex(env))
	else:
		return "".join(address+"\t"+receivers+"\t"+(senders or "")+"\n" for address, receivers, senders in get_mail_aliases(env))

@app.route('/mail/aliases/add', methods=['POST'])
@authorized_personnel_only
def mail_aliases_add():
	return add_mail_alias(
		request.form.get('address', ''),
		request.form.get('description', ''),
		request.form.get('forwards_to', ''),
		request.form.get('permitted_senders', ''),
		env,
		update_if_exists=(request.form.get('update_if_exists', '') == '1')
		)

@app.route('/mail/aliases/remove', methods=['POST'])
@authorized_personnel_only
def mail_aliases_remove():
	return remove_mail_alias(request.form.get('address', ''), env)

@app.route('/mail/domains')
@authorized_personnel_only
def mail_domains():
    return "".join(x+"\n" for x in get_mail_domains(env))

# DNS

@app.route('/dns/zones')
@authorized_personnel_only
def dns_zones():
	from dns_update import get_dns_zones
	return json_response([z[0] for z in get_dns_zones(env)])

@app.route('/dns/update', methods=['POST'])
@authorized_personnel_only
def dns_update():
	from dns_update import do_dns_update
	try:
		return do_dns_update(env, force=request.form.get('force', '') == '1')
	except Exception as e:
		return (str(e), 500)

@app.route('/dns/secondary-nameserver')
@authorized_personnel_only
def dns_get_secondary_nameserver():
	from dns_update import get_custom_dns_config, get_secondary_dns
	return json_response({ "hostnames": get_secondary_dns(get_custom_dns_config(env), mode=None) })

@app.route('/dns/secondary-nameserver', methods=['POST'])
@authorized_personnel_only
def dns_set_secondary_nameserver():
	from dns_update import set_secondary_dns
	try:
		return set_secondary_dns([ns.strip() for ns in re.split(r"[, ]+", request.form.get('hostnames') or "") if ns.strip() != ""], env)
	except ValueError as e:
		return (str(e), 400)

@app.route('/dns/custom')
@authorized_personnel_only
def dns_get_records(qname=None, rtype=None):
	from dns_update import get_custom_dns_config
	return json_response([
	{
		"qname": r[0],
		"rtype": r[1],
		"value": r[2],
	}
	for r in get_custom_dns_config(env)
	if r[0] != "_secondary_nameserver"
		and (not qname or r[0] == qname)
		and (not rtype or r[1] == rtype) ])

@app.route('/dns/custom/<qname>', methods=['GET', 'POST', 'PUT', 'DELETE'])
@app.route('/dns/custom/<qname>/<rtype>', methods=['GET', 'POST', 'PUT', 'DELETE'])
@authorized_personnel_only
def dns_set_record(qname, rtype="A"):
	from dns_update import do_dns_update, set_custom_dns_record
	try:
		# Normalize.
		rtype = rtype.upper()

		# Read the record value from the request BODY, which must be
		# ASCII-only. Not used with GET.
		value = request.stream.read().decode("ascii", "ignore").strip()

		if request.method == "GET":
			# Get the existing records matching the qname and rtype.
			return dns_get_records(qname, rtype)

		elif request.method in ("POST", "PUT"):
			# There is a default value for A/AAAA records.
			if rtype in ("A", "AAAA") and value == "":
				value = request.environ.get("HTTP_X_FORWARDED_FOR") # normally REMOTE_ADDR but we're behind nginx as a reverse proxy

			# Cannot add empty records.
			if value == '':
				return ("No value for the record provided.", 400)

			if request.method == "POST":
				# Add a new record (in addition to any existing records
				# for this qname-rtype pair).
				action = "add"
			elif request.method == "PUT":
				# In REST, PUT is supposed to be idempotent, so we'll
				# make this action set (replace all records for this
				# qname-rtype pair) rather than add (add a new record).
				action = "set"

		elif request.method == "DELETE":
			if value == '':
				# Delete all records for this qname-type pair.
				value = None
			else:
				# Delete just the qname-rtype-value record exactly.
				pass
			action = "remove"

		if set_custom_dns_record(qname, rtype, value, action, env):
			return do_dns_update(env) or "Something isn't right."
		return "OK"

	except ValueError as e:
		return (str(e), 400)

@app.route('/dns/dump')
@authorized_personnel_only
def dns_get_dump():
	from dns_update import build_recommended_dns
	return json_response(build_recommended_dns(env))

# SSL

@app.route('/ssl/status')
@authorized_personnel_only
def ssl_get_status():
	from ssl_certificates import get_certificates_to_provision
	from web_update import get_web_domains_info, get_web_domains

	# What domains can we provision certificates for? What unexpected problems do we have?
	provision, cant_provision = get_certificates_to_provision(env, show_valid_certs=False)

	# What's the current status of TLS certificates on all of the domain?
	domains_status = get_web_domains_info(env)
	domains_status = [
		{
			"domain": d["domain"],
			"status": d["ssl_certificate"][0],
			"text": d["ssl_certificate"][1] + ((" " + cant_provision[d["domain"]] if d["domain"] in cant_provision else ""))
		} for d in domains_status ]

	# Warn the user about domain names not hosted here because of other settings.
	for domain in set(get_web_domains(env, exclude_dns_elsewhere=False)) - set(get_web_domains(env)):
		domains_status.append({
			"domain": domain,
			"status": "not-applicable",
			"text": "The domain's website is hosted elsewhere.",
		})

	return json_response({
		"can_provision": utils.sort_domains(provision, env),
		"status": domains_status,
	})

@app.route('/ssl/csr/<domain>', methods=['POST'])
@authorized_personnel_only
def ssl_get_csr(domain):
	from ssl_certificates import create_csr
	ssl_private_key = os.path.join(os.path.join(env["STORAGE_ROOT"], 'ssl', 'ssl_private_key.pem'))
	return create_csr(domain, ssl_private_key, request.form.get('countrycode', ''), env)

@app.route('/ssl/install', methods=['POST'])
@authorized_personnel_only
def ssl_install_cert():
	from web_update import get_web_domains
	from ssl_certificates import install_cert
	domain = request.form.get('domain')
	ssl_cert = request.form.get('cert')
	ssl_chain = request.form.get('chain')
	if domain not in get_web_domains(env):
		return "Invalid domain name."
	return install_cert(domain, ssl_cert, ssl_chain, env)

@app.route('/ssl/provision', methods=['POST'])
@authorized_personnel_only
def ssl_provision_certs():
	from ssl_certificates import provision_certificates
	requests = provision_certificates(env, limit_domains=None)
	return json_response({ "requests": requests })

# multi-factor auth

@app.route('/mfa/status', methods=['GET'])
@authorized_personnel_only
def mfa_get_status():
	return json_response({
		"enabled_mfa": get_mfa_state(request.user_email, env),
		"new_mfa": {
			"totp": mfa_totp.provision(request.user_email, env)
		}
	})

@app.route('/mfa/totp/enable', methods=['POST'])
@authorized_personnel_only
def totp_post_enable():
	secret = request.form.get('secret')
	token = request.form.get('token')
	label = request.form.get('label')
	if type(token) != str:
		return json_response({ "error": 'bad_input' }, 400)
	try:
		mfa_totp.validate_secret(secret)
		enable_mfa(request.user_email, "totp", secret, token, label, env)
	except ValueError as e:
		return str(e)
	return "OK"

@app.route('/mfa/disable', methods=['POST'])
@authorized_personnel_only
def totp_post_disable():
	disable_mfa(request.user_email, request.form.get('mfa-id'), env)
	return "OK"

# WEB

@app.route('/web/domains')
@authorized_personnel_only
def web_get_domains():
	from web_update import get_web_domains_info
	return json_response(get_web_domains_info(env))

@app.route('/web/update', methods=['POST'])
@authorized_personnel_only
def web_update():
	from web_update import do_web_update
	return do_web_update(env)

# System

@app.route('/system/version', methods=["GET"])
@authorized_personnel_only
def system_version():
	from status_checks import what_version_is_this
	try:
		return what_version_is_this(env)
	except Exception as e:
		return (str(e), 500)

@app.route('/system/latest-upstream-version', methods=["POST"])
@authorized_personnel_only
def system_latest_upstream_version():
	from status_checks import get_latest_miab_version
	try:
		return get_latest_miab_version()
	except Exception as e:
		return (str(e), 500)

@app.route('/system/status', methods=["POST"])
@authorized_personnel_only
def system_status():
	from status_checks import run_checks
	class WebOutput:
		def __init__(self):
			self.items = []
		def add_heading(self, heading):
			self.items.append({ "type": "heading", "text": heading, "extra": [] })
		def print_ok(self, message):
			self.items.append({ "type": "ok", "text": message, "extra": [] })
		def print_error(self, message):
			self.items.append({ "type": "error", "text": message, "extra": [] })
		def print_warning(self, message):
			self.items.append({ "type": "warning", "text": message, "extra": [] })
		def print_line(self, message, monospace=False):
			self.items[-1]["extra"].append({ "text": message, "monospace": monospace })
	output = WebOutput()
	# Create a temporary pool of processes for the status checks
	with multiprocessing.pool.Pool(processes=5) as pool:
		run_checks(False, env, output, pool)
	return json_response(output.items)

@app.route('/system/updates')
@authorized_personnel_only
def show_updates():
	from status_checks import list_apt_updates
	return "".join(
		"%s (%s)\n"
		% (p["package"], p["version"])
		for p in list_apt_updates())

@app.route('/system/update-packages', methods=["POST"])
@authorized_personnel_only
def do_updates():
	utils.shell("check_call", ["/usr/bin/apt-get", "-qq", "update"])
	return utils.shell("check_output", ["/usr/bin/apt-get", "-y", "upgrade"], env={
		"DEBIAN_FRONTEND": "noninteractive"
	})


@app.route('/system/reboot', methods=["GET"])
@authorized_personnel_only
def needs_reboot():
	from status_checks import is_reboot_needed_due_to_package_installation
	if is_reboot_needed_due_to_package_installation():
		return json_response(True)
	else:
		return json_response(False)

@app.route('/system/reboot', methods=["POST"])
@authorized_personnel_only
def do_reboot():
	# To keep the attack surface low, we don't allow a remote reboot if one isn't necessary.
	from status_checks import is_reboot_needed_due_to_package_installation
	if is_reboot_needed_due_to_package_installation():
		return utils.shell("check_output", ["/sbin/shutdown", "-r", "now"], capture_stderr=True)
	else:
		return "No reboot is required, so it is not allowed."


@app.route('/system/backup/status')
@authorized_personnel_only
def backup_status():
	from backup import backup_status
	try:
		return json_response(backup_status(env))
	except Exception as e:
		return json_response({ "error": str(e) })

@app.route('/system/backup/config', methods=["GET"])
@authorized_personnel_only
def backup_get_custom():
	from backup import get_backup_config
	return json_response(get_backup_config(env, for_ui=True))

@app.route('/system/backup/config', methods=["POST"])
@authorized_personnel_only
def backup_set_custom():
	from backup import backup_set_custom
	return json_response(backup_set_custom(env,
		request.form.get('target', ''),
		request.form.get('target_user', ''),
		request.form.get('target_pass', ''),
		request.form.get('min_age', '')
	))

@app.route('/system/privacy', methods=["GET"])
@authorized_personnel_only
def privacy_status_get():
	config = utils.load_settings(env)
	return json_response(config.get("privacy", True))

@app.route('/system/privacy', methods=["POST"])
@authorized_personnel_only
def privacy_status_set():
	config = utils.load_settings(env)
	config["privacy"] = (request.form.get('value') == "private")
	utils.write_settings(config, env)
	return "OK"

# MUNIN

@app.route('/munin/')
@app.route('/munin/<path:filename>')
@authorized_personnel_only
def munin(filename=""):
	# Checks administrative access (@authorized_personnel_only) and then just proxies
	# the request to static files.
	if filename == "": filename = "index.html"
	return send_from_directory("/var/cache/munin/www", filename)

@app.route('/munin/cgi-graph/<path:filename>')
@authorized_personnel_only
def munin_cgi(filename):
	""" Relay munin cgi dynazoom requests
	/usr/lib/munin/cgi/munin-cgi-graph is a perl cgi script in the munin package
	that is responsible for generating binary png images _and_ associated HTTP
	headers based on parameters in the requesting URL. All output is written
	to stdout which munin_cgi splits into response headers and binary response
	data.
	munin-cgi-graph reads environment variables to determine
	what it should do. It expects a path to be in the env-var PATH_INFO, and a
	querystring to be in the env-var QUERY_STRING.
	munin-cgi-graph has several failure modes. Some write HTTP Status headers and
	others return nonzero exit codes.
	Situating munin_cgi between the user-agent and munin-cgi-graph enables keeping
	the cgi script behind mailinabox's auth mechanisms and avoids additional
	support infrastructure like spawn-fcgi.
	"""

	COMMAND = 'su - munin --preserve-environment --shell=/bin/bash -c /usr/lib/munin/cgi/munin-cgi-graph'
	# su changes user, we use the munin user here
	# --preserve-environment retains the environment, which is where Popen's `env` data is
	# --shell=/bin/bash ensures the shell used is bash
	# -c "/usr/lib/munin/cgi/munin-cgi-graph" passes the command to run as munin
	# "%s" is a placeholder for where the request's querystring will be added

	if filename == "":
		return ("a path must be specified", 404)

	query_str = request.query_string.decode("utf-8", 'ignore')

	env = {'PATH_INFO': '/%s/' % filename, 'REQUEST_METHOD': 'GET', 'QUERY_STRING': query_str}
	code, binout = utils.shell('check_output',
							   COMMAND.split(" ", 5),
							   # Using a maxsplit of 5 keeps the last arguments together
							   env=env,
							   return_bytes=True,
							   trap=True)

	if code != 0:
		# nonzero returncode indicates error
		app.logger.error("munin_cgi: munin-cgi-graph returned nonzero exit code, %s", code)
		return ("error processing graph image", 500)

	# /usr/lib/munin/cgi/munin-cgi-graph returns both headers and binary png when successful.
	# A double-Windows-style-newline always indicates the end of HTTP headers.
	headers, image_bytes = binout.split(b'\r\n\r\n', 1)
	response = make_response(image_bytes)
	for line in headers.splitlines():
		name, value = line.decode("utf8").split(':', 1)
		response.headers[name] = value
	if 'Status' in response.headers and '404' in response.headers['Status']:
		app.logger.warning("munin_cgi: munin-cgi-graph returned 404 status code. PATH_INFO=%s", env['PATH_INFO'])
	return response

def log_failed_login(request):
	# We need to figure out the ip to list in the message, all our calls are routed
	# through nginx who will put the original ip in X-Forwarded-For.
	# During setup we call the management interface directly to determine the user
	# status. So we can't always use X-Forwarded-For because during setup that header
	# will not be present.
	if request.headers.getlist("X-Forwarded-For"):
		ip = request.headers.getlist("X-Forwarded-For")[0]
	else:
		ip = request.remote_addr

	# We need to add a timestamp to the log message, otherwise /dev/log will eat the "duplicate"
	# message.
	app.logger.warning( "Mail-in-a-Box Management Daemon: Failed login attempt from ip %s - timestamp %s" % (ip, time.time()))


# APP

if __name__ == '__main__':
	if "DEBUG" in os.environ: app.debug = True
	if "APIKEY" in os.environ: auth_service.key = os.environ["APIKEY"]

	if not app.debug:
		app.logger.addHandler(utils.create_syslog_handler())

	# For testing on the command line, you can use `curl` like so:
	#    curl --user $(</var/lib/mailinabox/api.key): http://localhost:10222/mail/users
	auth_service.write_key()

	# For testing in the browser, you can copy the API key that's output to the
	# debug console and enter that as the username
	app.logger.info('API key: ' + auth_service.key)

	# Start the application server. Listens on 127.0.0.1 (IPv4 only).
	app.run(port=10222)
