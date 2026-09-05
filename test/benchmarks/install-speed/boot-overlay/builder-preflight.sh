#!/bin/bash
# The payload must include a freshly generated disposable PUBLIC key only.
set -euo pipefail
[[ $(systemd-detect-virt --vm) =~ ^(qemu|kvm)$ ]]
[[ -e /run/archiso/bootmnt/arch/x86_64/airootfs.sfs ]]
install -dm 700 /root/.ssh
install -m 600 /usr/local/lib/omarchy-benchmark/builder-key.pub /root/.ssh/authorized_keys
install -dm 755 /etc/ssh/sshd_config.d
cat >/etc/ssh/sshd_config.d/00-omarchy-benchmark-builder.conf <<'EOF'
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
ssh-keygen -A
systemctl start sshd
echo 'Disposable builder SSH enabled; no installer was started.'
