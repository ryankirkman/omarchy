#!/bin/bash
# Run only after smoke timing, installed boot and standalone reboot are sealed.
set -euo pipefail
if (( $# != 1 )); then echo 'Usage: inspect-sealed-source.sh <read-only-hotplug-device>' >&2; exit 2; fi
device=$(readlink -f "$1")
[[ -b $device ]]
[[ $(systemd-detect-virt --vm) =~ ^(qemu|kvm)$ ]]
[[ $(lsblk -dnro SERIAL "$device" | tr -d '[:space:]') == OMARCHY_SEALED_SOURCE ]]
[[ $(blockdev --getro "$device") == 1 ]]
source_uuid=$(blkid -s UUID -o value "$device")
installed_uuid=$(findmnt -no UUID /)
[[ $source_uuid == 51a18a79-7bde-4aeb-8895-181970407957 ]]
[[ -n $installed_uuid && $installed_uuid != "$source_uuid" ]]
[[ -z $(lsblk -nro MOUNTPOINTS "$device" | tr -d '[:space:]') ]]
mount_dir=$(mktemp -d /run/omarchy-sealed-inspect.XXXXXX)
cleanup() { status=$?; set +e; if mountpoint -q "$mount_dir"; then umount "$mount_dir"; fi; rmdir "$mount_dir"; exit "$status"; }
trap cleanup EXIT
mount -o ro,rescue=nologreplay,subvolid=5 "$device" "$mount_dir"
[[ $(btrfs property get -ts "$mount_dir/omarchy-root" ro) == 'ro=true' ]]
python - "$mount_dir/omarchy-root" "$source_uuid" "$installed_uuid" <<'PY'
import json,posixpath,stat,sys
from pathlib import Path
root=Path(sys.argv[1]);source_uuid=sys.argv[2];installed_uuid=sys.argv[3]
def observe(relative):
 path=root/relative
 parent=root
 for part in Path(relative).parts[:-1]:
  parent=parent/part
  try:parent_info=parent.lstat()
  except FileNotFoundError:return {'exists':False}
  if not stat.S_ISDIR(parent_info.st_mode):return {'exists':None,'blocked_parent':str(parent.relative_to(root))}
 try:info=path.lstat()
 except FileNotFoundError:return {'exists':False}
 result={'exists':True,'bytes':info.st_size,'mode':oct(stat.S_IMODE(info.st_mode))}
 if stat.S_ISLNK(info.st_mode):result.update(type='symlink',target=str(path.readlink()))
 elif stat.S_ISREG(info.st_mode):result['type']='regular'
 elif stat.S_ISDIR(info.st_mode):result['type']='directory'
 else:result['type']='other'
 return result
observations={name:observe(name) for name in ['etc/machine-id','var/lib/systemd/random-seed','etc/machine-info','var/lib/dbus/machine-id']}
dbus=observations['var/lib/dbus/machine-id']
if dbus.get('type')=='symlink':
 target=dbus['target']
 # Resolve lexically in the source filesystem namespace. Never follow an
 # absolute source symlink with host/installed-root Path.resolve/open.
 if target.startswith('/'):
  resolved=posixpath.normpath(target)
 else:
  resolved=posixpath.normpath('/var/lib/dbus/'+target)
 dbus['source_namespace_target']=resolved
 dbus['links_to_source_machine_id']=resolved=='/etc/machine-id'
 if resolved=='/etc/machine-id':dbus['source_target_observation']=observations['etc/machine-id']
seed=observations['var/lib/systemd/random-seed'];machine=observations['etc/machine-id']
issues=[name+': parent path is not a source directory' for name,value in observations.items() if 'blocked_parent' in value]
if machine.get('type')!='regular' or machine.get('bytes')!=0:issues.append('source machine ID is not an empty regular file')
if seed.get('exists') and not(seed.get('type')=='regular' and seed.get('bytes')==0):issues.append('random seed exists; inspect its role before accepting cloned identities')
if observations['etc/machine-info'].get('exists'):issues.append('machine-info exists; its content has not been inspected')
if not dbus.get('links_to_source_machine_id'):issues.append('dbus identity is not the expected link to the empty source machine ID')
result={'schema_version':1,'type':'untimed post-validation sealed source inspection','source_filesystem_uuid':source_uuid,'installed_root_filesystem_uuid':installed_uuid,'device_readonly_asserted':True,'source_subvolume_readonly_asserted':True,'mount_options':'ro,rescue=nologreplay,subvolid=5','observations':observations,'source_file_contents_read':False,'requires_review':issues}
print(json.dumps(result,indent=2))
PY
