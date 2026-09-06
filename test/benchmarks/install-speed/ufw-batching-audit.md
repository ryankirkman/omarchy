# UFW batching: rejected source-only lead

No supported batching change was found that preserves all existing rules, errors and active/inactive behavior. This audit changes no firewall, installer or benchmark input. The approximately 21- and 19-second `config/firewall.sh` spans in two old local TCG logs are coarse guest wall-clock observations, not native timings or attribution to individual UFW invocations. No native gain is established.

The source is the official Omarchy 4.0.2 ISO, SHA256 `2ef8e624aa1bec7e277e28056b8535a6c9373ba48d7ede3f1a01cb6d2373cfb8`, containing `ufw 0.36.2-7` and `omarchy 4.0.2-1`. Selected package members were streamed from the offline archives and checked against their MTREE SHA256 records. No UFW command was executed, and no full source extraction or raw log is retained here.

| Package member | SHA256 |
| --- | --- |
| `omarchy`: `usr/share/omarchy/install/config/firewall.sh` | `c15b76478355ae633b990520b7f8db829e2c3ab4a850922bb2c75072a99d4fcd` |
| `ufw`: `usr/bin/ufw` | `fe7cb9f8beb4c6f59b785fea5482c50d2e2ae98ec1c6d124cce38375f133c329` |
| `ufw`: `usr/lib/python3.14/site-packages/ufw/backend.py` | `24a372164947a8a3808b8797db49430d285f6ec1113f2c2895e403a058689232` |
| `ufw`: `usr/lib/python3.14/site-packages/ufw/backend_iptables.py` | `69cabdd144d3feef41910ac44fd79dde9fd518aa2c9eb8d9e6a7800c4661fc55` |
| `ufw`: `usr/lib/python3.14/site-packages/ufw/frontend.py` | `c61d78d719778acf01314b222078db152fbfb45b2f9de66f3df51be45868d7eb` |
| `ufw`: `usr/share/man/man8/ufw.8.gz` | `c5728f68a78eb1a04d17c8ef51d67153eb1a461959b74279c00008a14158ccd3` |

The firewall leaf, also byte-identical in the reviewed fork, invokes two default-policy commands and four allow commands before `ufw-docker`. The CLI parses one action, creates one frontend/backend, performs that action under a lock, and exits; it exposes no batch-file or stdin command interface. Backend initialization repeats permission/configuration checks, defaults/rules/profile reads and iptables version detection (`backend.py:32–68`); rule changes also initialize netfilter capabilities (`backend_iptables.py:954–964`). Reusing private Python objects to avoid that initialization is outside this lead's scope.

The packaged manual documents `ufw allow 53317` as allowing both TCP and UDP. However, this represents one combined rule expanded into TCP then UDP (`backend_iptables.py:598–618`), instead of the existing separately managed UDP then TCP rules. It changes rule identity, order, duplicate matching and possible partial-failure behavior. Equal final packet permissions alone do not establish equivalent rule-management behavior.

The packaged default policies already specify incoming DROP and outgoing ACCEPT, but skipping the two commands would omit their validation and write failures. On an active firewall, `frontend.py:245–254` also stops and starts the firewall after setting a default policy. The existing ISO leaf deliberately keeps the live installer's firewall inactive, installs the Docker configuration through its status-only shim, and enables UFW for the installed system's boot; that behavior must remain intact. Neither shortcut is proposed for implementation.
