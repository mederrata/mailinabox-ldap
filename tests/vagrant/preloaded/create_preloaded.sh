#!/bin/bash

# load defaults for MIABLDAP_RELEASE_TAG and MIABLDAP_GIT
pushd "../../.." >/dev/null
source tests/system-setup/setup-defaults.sh || exit 1
popd >/dev/null

# TODO: replace MIABLDAP_RELEASE_TAG with the actual tag for the last supported version of miabldap for bionic64
UBUNTU_BIONIC64_RELEASE_TAG=$MIABLDAP_RELEASE_TAG

vagrant destroy -f
rm -f prepcode.txt

for plugin in "vagrant-vbguest" "vagrant-reload"
do
    if ! vagrant plugin list | grep -F "$plugin" >/dev/null; then
        vagrant plugin install "$plugin" || exit 1
    fi
done

vagrant box update


boxes=(
    "preloaded-ubuntu-bionic64"
    "preloaded-ubuntu-jammy64"
)
# preload packages from source of the following git tags. empty string
# means use the current source tree
tags=(
    "$UBUNTU_BIONIC64_RELEASE_TAG"
    ""
)
try_reboot=(
    false
    true
)
idx=0

for box in "${boxes[@]}"
do
    if [ ! -z "$1" -a "$1" != "$box" ]; then
        continue
    fi

    export RELEASE_TAG="${tags[$idx]}"
    vagrant up $box
    upcode=$?
    if [ $upcode -ne 0 -a ! -e "./prepcode.txt" ] && ${try_reboot[$idx]}
    then
        # a reboot may be necessary if guest addtions was newly
        # compiled by vagrant plugin "vagrant-vbguest"
        echo ""
        echo "VAGRANT UP RETURNED $upcode -- RETRYING AFTER REBOOT"
        vagrant halt $box
        vagrant up $box
        upcode=$?
    fi
        
    let idx+=1
    prepcode=$(cat "./prepcode.txt")
    rm -f prepcode.txt
    echo ""
    echo "VAGRANT UP RETURNED $upcode"
    echo "PREPVM RETURNED $prepcode"

    if [ "$prepcode" != "0" -o $upcode -ne 0 ]; then
        echo "FAILED!!!!!!!!"
        vagrant destroy -f $box
        exit 1
    fi

    if vagrant ssh $box -- cat /var/run/reboot-required; then
        vagrant reload $box
    fi

    vagrant halt $box
    vagrant package $box
    rm -f $box.box
    mv package.box $box.box

    vagrant destroy -f $box
    cached_name="$(sed 's/preloaded-/preloaded-miabldap-/' <<<"$box")"
    echo "Removing cached box $cached_name"
    if [ -e "../funcs.rb" ]; then
        pushd .. > /dev/null
        vagrant box remove $cached_name
        code=$?
        popd > /dev/null
    else
        vagrant box remove $cached_name
        code=$?
    fi
    echo "Remove cache box result: $code - ignoring"
done
