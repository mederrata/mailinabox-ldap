#!/bin/bash
setup/ehdd/mount.sh || exit 1

if [ -s /etc/mailinabox.conf ]; then
    [ -x /usr/sbin/slapd ] && systemctl start slapd
    systemctl start php7.2-fpm
    systemctl start dovecot
    systemctl start postfix
    systemctl start nginx
    systemctl start cron
    #systemctl start nsd
    systemctl link -f $HOME/mailinabox/conf/mailinabox.service
    systemctl start mailinabox
    systemctl start fail2ban
fi

