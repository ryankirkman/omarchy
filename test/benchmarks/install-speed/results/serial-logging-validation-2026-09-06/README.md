# Serial system logging candidate preparation

The actual native preparation path passed all 56 selected component checks: 13 indexing, 14 animation, seven firewall, nine logging guard and 13 logging payload/phase checks. Two focused native checks also passed for separate provenance/scope and actual four-variant initramfs packaging. Existing selection, default, failure-before-download and stale-input checks are retained with the preceding integration record.

The selected logging scope is exactly `serial-system-finalizer-only`. Only system setup opts into the private logger bind; the firewall, user and index calls keep their original private chroot path. This follows the rejected all-call component experiment and uses separately measured fresh guest samples.

`preparation.json` records the 36 source hashes and modes, pinned upstream revision, final native-driver hash and successful contract-log digests. Preparation verified those inputs again, then composed both new layers over the existing full-overlap payload. All 18 inherited files retained their bytes and modes. The final 30-file payload manifest is `01cf1741ac9de4bcde45c516620663ec85b2dd082cea545d9f06fc3423e96ffa`; activation script SHA256 is `70842c07219139c13d7ef742d47aee35367477dab5577c697fdaf99e224ad586`; final phases SHA256 is `957640cf806e7b5915f6cd6f9c21c2a2d4844ae8696429788b937fe357b0ff5e`.

These are host correctness and composition results. No VM, image download, target installation or install-speed measurement was performed by this check. The real guest namespace and component timing evidence is preserved separately by the logging and firewall components. Only fresh native full installations can establish the complete speedup.
