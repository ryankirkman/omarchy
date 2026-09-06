# Early component validation integration check

The real native validation factory ran all 13 indexing and 14 dashboard/payload contracts successfully, then passed its result to actual later payload preparation. The contract logs remained byte-for-byte unchanged during preparation. All 18 final payload files matched their recorded hashes and modes, and the complete final manifest is byte-identical to the earlier verified overlap payload (`0c7563d93d0ea95c2104bf7fbec2b50acb64dce304a32f1f55b54a6123729959`). Moving these checks earlier changes test scheduling only.

The source inputs, selected upstream commit, contract logs and native-driver digest are retained. Focused native contracts separately verify failure before downloads/installation, matching context, source/log drift and unchanged diagnostic/default behavior. This local integration check did not download an ISO, launch a VM or measure installation speed. The active native trial remains pinned to its earlier published commit.
