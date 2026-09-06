#!/bin/bash
# Disposable live-ISO fixture only; never run on a host or installed target.
set -euo pipefail
[[ $(systemd-detect-virt --vm) =~ ^(qemu|kvm)$ ]]
[[ -f /run/archiso/bootmnt/arch/x86_64/airootfs.sfs ]]
[[ $(findmnt -n -o FSTYPE /) == overlay ]]
[[ -f /root/.automated_script.benchmark-original.sh ]]
[[ ! -e /run/omarchy-console-fixture-ready ]]

hostnamectl set-hostname omarchy-benchmark
# No SSH key is installed. Keep the fixture password restricted to the console.
systemctl stop sshd.service
systemctl mask --runtime sshd.service
printf '%s\n' 'root:omarchy' | chpasswd
usermod --shell /usr/bin/bash root
cat > /root/.bash_profile <<'EOF'
unset PROMPT_COMMAND PS0
PS1='[root@\h \W]# '
HISTFILE=/dev/null
EOF

systemctl enable --runtime --now serial-getty@ttyS0.service
[[ $(systemctl is-enabled sshd.service) == masked-runtime ]]
[[ $(systemctl is-active serial-getty@ttyS0.service) == active ]]
[[ -z $(pgrep -af '^(python|python3) -m orchestrator.main' || true) ]]
touch /run/omarchy-console-fixture-ready
printf '%s\n' 'OMARCHY_CONSOLE_FIXTURE_READY builder_no_autoinstall root_bash sshd_masked' > /dev/ttyS0
