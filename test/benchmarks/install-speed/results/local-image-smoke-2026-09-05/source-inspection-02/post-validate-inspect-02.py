import hashlib, json, pathlib, shlex, time
root=pathlib.Path('/tmp/omarchy-bench'); run=root/'candidate-firmware-smoke-02'
manifest=json.loads((run/'manifest.json').read_text())
assert manifest['status']=='installed-and-booted' and manifest['validation_passed'] and manifest['standalone_reboot_passed']
assert manifest['measurement_interrupted'] is False
backend=json.loads((root/'supplemental-revision-02/sealed-source-byte-range.json').read_text())
assert backend['root_image_sha256']=='6ef64246e8b7d01e8f129046bae0d8e41228f7f195fd4c1ffb8fe00e4d00ca3e'
assert backend['iso']==str(root/'fast-image-fixed.iso') and backend['offset_bytes']==116736
out=run/'sealed-source-inspection';out.mkdir()
(out/'pre-inspection-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
(out/'source-byte-range.json').write_text(json.dumps(backend,indent=2)+'\n')
script=(root/'inspect-sealed-source.sh').read_text();(out/'inspect-sealed-source.sh').write_text(script)
sequence=0
def request(value):
 global sequence
 sequence+=1;name=f'sealed-source-{sequence:02d}.json'; path=run/'requests'/name
 assert not path.exists()
 tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,indent=2)+'\n');tmp.rename(path)
 deadline=time.monotonic()+180
 while time.monotonic()<deadline:
  response=run/'responses'/name
  if response.exists():
   result=json.loads(response.read_text());(out/name).write_text(json.dumps({'request':value,'response':result},indent=2)+'\n')
   assert result['ok'],result
   return result['result']
  time.sleep(.5)
 raise RuntimeError('Timed out waiting for '+name)
request({'action':'qmp','execute':'blockdev-add','arguments':backend['read_only_blockdev']})
request({'action':'qmp','execute':'device_add','arguments':{'driver':'usb-storage','drive':'omarchy-sealed-source','id':'omarchy-sealed-source-usb','serial':'OMARCHY_SEALED_SOURCE'}})
detect="""set -euo pipefail
for attempt in {1..100}; do
 mapfile -t devices < <(lsblk -dnro PATH,SERIAL | awk '$2 == "OMARCHY_SEALED_SOURCE" {print $1}')
 (( ${#devices[@]} == 1 )) && break
 sleep .2
done
(( ${#devices[@]} == 1 ))
device=${devices[0]}
"""
command=detect+'bash -c '+shlex.quote(script)+' -- "$device"'
result=request({'action':'ssh','sudo':True,'command':command,'timeout':120})
assert result['returncode']==0,result
observation=json.loads(result['stdout']);(run/'sealed-source-identity-observation.json').write_text(json.dumps(observation,indent=2)+'\n')
assert observation['device_readonly_asserted'] and observation['source_subvolume_readonly_asserted'] and not observation['source_file_contents_read']
cleanup=detect+"""[[ -z $(lsblk -nro MOUNTPOINTS "$device" | tr -d '[:space:]') ]]
mounted_uuids=$(findmnt -rn -o UUID)
while IFS= read -r uuid; do [[ $uuid != 51a18a79-7bde-4aeb-8895-181970407957 ]] || exit 1; done <<< "$mounted_uuids"
for path in /run/omarchy-sealed-inspect.*; do [[ ! -e $path ]]; done
printf 'SOURCE_UNMOUNTED\n'
"""
result=request({'action':'ssh','sudo':True,'command':cleanup,'timeout':60});assert result['returncode']==0 and result['stdout'].strip()=='SOURCE_UNMOUNTED',result
request({'action':'qmp','execute':'device_del','arguments':{'id':'omarchy-sealed-source-usb'}})
result=request({'action':'ssh','sudo':True,'command':"set -euo pipefail; udevadm settle; [[ -z $(lsblk -dnro SERIAL | awk '$1 == \"OMARCHY_SEALED_SOURCE\" {print $1}') ]]; printf 'SOURCE_DETACHED\\n'",'timeout':60})
assert result['returncode']==0 and result['stdout'].strip()=='SOURCE_DETACHED',result
request({'action':'qmp','execute':'blockdev-del','arguments':{'node-name':'omarchy-sealed-source'}})
(out/'complete.json').write_text(json.dumps({'status':'complete','observed_after_all_install_gates':True,'source_unmounted':True,'usb_detached':True,'backend_deleted':True,'requires_review':observation['requires_review'],'script_sha256':hashlib.sha256(script.encode()).hexdigest(),'completed_at':time.time()},indent=2)+'\n')
print(json.dumps({'status':'complete','observation':str(run/'sealed-source-identity-observation.json'),'requires_review':observation['requires_review']}),flush=True)

assert observation['requires_review']==[],observation['requires_review']
