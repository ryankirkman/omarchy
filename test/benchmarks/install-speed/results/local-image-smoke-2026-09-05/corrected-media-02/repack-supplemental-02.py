import hashlib, json, os, pathlib, re, shutil, subprocess, time
root=pathlib.Path('/tmp/omarchy-bench')
old=root/'fast-image.iso'; new=root/'fast-image-fixed.iso'; evidence=root/'supplemental-revision-02'
bundles=root/'subvolume-fixed-bundles-v2'
assert not new.exists() and not evidence.exists()
assert shutil.disk_usage(root).free > old.stat().st_size + 1024**3
assert old.stat().st_size == 3697973248
evidence.mkdir()
env=os.environ.copy(); env['LD_LIBRARY_PATH']=str(root/'toolchain/usr/lib/x86_64-linux-gnu')
xorriso=str(root/'toolchain/usr/bin/xorriso')
changes=['installer-overlay.tar','installer-overlay.tar.sha256','installer-overlay.manifest.json']
argv=[xorriso,'-indev',str(old),'-outdev',str(new)]
for name in changes: argv += ['-map',str(bundles/name),'/'+name]
argv+=['-commit','-end']
(evidence/'repack-command.json').write_text(json.dumps(argv,indent=2)+'\n')
with (evidence/'repack.log').open('w') as log: subprocess.run(argv,env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
def inventory(iso):
 result=subprocess.run([xorriso,'-indev',str(iso),'-find','/','-type','f','-exec','report_sections','--'],env=env,check=True,capture_output=True,text=True)
 (evidence/(iso.stem+'-sections.txt')).write_text(result.stdout)
 pattern=re.compile(r"File data lba:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']+)'")
 sections={}
 for line in result.stdout.splitlines():
  m=pattern.fullmatch(line)
  if m:
   index,lba,blocks,length=map(int,m.groups()[:4]); sections.setdefault(m[5],[]).append((index,lba,blocks,length))
 assert len(sections)==10
 results={}
 with iso.open('rb') as stream:
  for name, extents in sections.items():
   extents.sort(); assert [v[0] for v in extents]==list(range(len(extents)))
   digest=hashlib.sha256(); size=0
   for _,lba,blocks,length in extents:
    assert 0<length<=blocks*2048 and lba*2048+length<=iso.stat().st_size
    stream.seek(lba*2048); remaining=length; size+=length
    while remaining:
     chunk=stream.read(min(8*1024**2,remaining)); assert chunk; digest.update(chunk); remaining-=len(chunk)
   results[name]={'sha256':digest.hexdigest(),'bytes':size,'sections':extents}
 return results
old_files=inventory(old); new_files=inventory(new)
assert old_files.keys()==new_files.keys()
for name,entry in old_files.items():
 if name.lstrip('/') in changes:
  replacement=bundles/name.lstrip('/')
  assert new_files[name]['sha256']==hashlib.sha256(replacement.read_bytes()).hexdigest()
 else:
  assert entry['sha256']==new_files[name]['sha256'] and entry['bytes']==new_files[name]['bytes']
member='/arch/x86_64/omarchy-root.btrfs.qcow2'
assert new_files[member]['sha256']=='6ef64246e8b7d01e8f129046bae0d8e41228f7f195fd4c1ffb8fe00e4d00ca3e'
subprocess.run(['python','/workspace/scratch/38a4d12ea30d/omarchy/test/benchmarks/install-speed/image/verify-image-media.py',str(new),str(root/'image-media/arch/x86_64/omarchy-root.btrfs.qcow2.json'),str(evidence/'image-media-verification.json'),'--xorriso',xorriso],env=env,check=True,stdout=subprocess.DEVNULL)
verification=json.loads((evidence/'image-media-verification.json').read_text())
record={'schema_version':1,'status':'verified','time':time.time(),'original_iso_sha256':'add515a8db48541779613d4071d7b140a9df64c07d79070f4b4472d13fe36892','new_iso_sha256':verification['iso_sha256'],'new_iso_bytes':new.stat().st_size,'changes':changes,'original_files':old_files,'new_files':new_files,'source_root_image_unchanged':True,'patch_commit':'9512167'}
(evidence/'supplemental-revision.json').write_text(json.dumps(record,indent=2)+'\n')
shutil.copy(root/'image-media-verification.json',evidence/'original-image-media-verification.json')
for name in changes: shutil.copy(bundles/name,evidence/name)
sections=new_files[member]['sections']; assert len(sections)==1
_,lba,_,size=sections[0]; offset=lba*2048
opts=f'driver=qcow2,file.driver=raw,file.offset={offset},file.size={size},file.file.driver=file,file.file.filename={new}'
info=json.loads(subprocess.check_output([str(root/'toolchain/usr/bin/qemu-img'),'info','--output=json','--image-opts',opts],env=env))
assert info['virtual-size']==6174015488 and info['format']=='qcow2'
backend={'driver':'qcow2','node-name':'omarchy-sealed-source','read-only':True,'file':{'driver':'raw','read-only':True,'offset':offset,'size':size,'file':{'driver':'file','read-only':True,'filename':str(new)}}}
(evidence/'sealed-source-byte-range.json').write_text(json.dumps({'iso':str(new),'iso_sha256':verification['iso_sha256'],'root_image_sha256':new_files[member]['sha256'],'member':member,'offset_bytes':offset,'size_bytes':size,'qemu_image_opts':opts,'qemu_info':info,'read_only_blockdev':backend},indent=2)+'\n')
print(json.dumps({'status':'verified','iso':str(new),'sha256':verification['iso_sha256'],'offset':offset,'source_unchanged':True}),flush=True)
