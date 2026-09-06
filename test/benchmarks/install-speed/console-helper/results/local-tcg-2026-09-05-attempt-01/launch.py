from pathlib import Path
import json, subprocess, time
p=Path(__file__).parent
a=json.loads((p/"launch.json").read_text())["argv"]
t=time.monotonic()
with (p/"supervisor.log").open("w") as f:
 r=subprocess.run(a,stdout=f,stderr=subprocess.STDOUT)
(p/"supervisor-completion.json").write_text(json.dumps({"returncode":r.returncode,"elapsed_s":time.monotonic()-t},indent=2)+"\n")
print((p/"supervisor-completion.json").read_text(),flush=True)
