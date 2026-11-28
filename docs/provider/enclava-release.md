# Enclava image release record (kind 8428)

An **8428** is the release team's signed statement that it released a specific
OCI image for Enclava. It is a regular (non-replaceable) Nostr event, with no
`d` tag. Retain its exact event ID; do not pick an arbitrary latest event. A
node references the ID in its own signed 38421 listing. The node does not
issue the release record, and a 38425 review need not change on upgrades.

## Wire format (v1)

The artifact `enclava-release-event.json` contains one standard signed Nostr
event (`kind: 8428`, `tags: []`). Its `content` is a JSON **string** whose
decoded object has these fields:

```json
{
  "schema": "routstr.enclava-image-release.v1",
  "scheme": "enclava-tee",
  "image": "ghcr.io/routstr/core-enclava@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "image_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

`image` is the exact qualified OCI reference, not a mutable tag or Git SHA.
`image_digest` is the same build output digest (lowercase hex). Consumers must
verify the event ID and signature, require the independently pinned release
pubkey, check kind/schema/scheme, and require the image and digest to agree
with the expected repository and deployment input. The public key in an
untrusted listing or downloaded artifact is not a trust anchor.

**Not a deployment approval.** The record contains no hostname, deployment ID,
measurement, TLS key or universal verifier policy. It does not assert the
image is running, authorize payment, or replace validation of cosign provenance.
A TEE-required client still needs independently approved verification inputs and
live evidence bound to the connection that will carry the request. The
single-deployment policy used to test a node is not a portable release policy.

## Publish and hand off to deployment

1. Configure the GitHub Actions **`enclava-release` environment** with a rule
   limiting deployments to `main` and required reviewer(s). Set its secret
   `ENCLAVA_RELEASE_NSEC` to the dedicated Routstr release signing key (never a
   personal key and never the node NSEC). Set environment variables
   `ENCLAVA_RELEASE_PUBKEY` to the expected lowercase 64-hex public key and
   `ENCLAVA_RELEASE_RELAY` to a public `wss://` relay URL without credentials.
   Distribute the release pubkey independently, out of band.

   The release key is a trust anchor: whoever holds it can mint an image
   release record for any digest. Hold it in the Routstr org (shared secret
   store), rotate it deliberately, and pin the new pubkey in client releases
   before the first record signed by the new key is published.

   Current Routstr release identity:

   ```
   pubkey (hex): a6f1b316ef3ec567ecc2f3325461dce3ac46d086e5b326c12802834b1c67cef4
   npub:         npub15mcmx9h08mzk0mxz7ve9gcwuuwkyd5yxukejdsfgq2p5k8r8em6qye9xyz
   ```

   The image package is `ghcr.io/routstr/core-enclava`, published with the
   repository's own `GITHUB_TOKEN` (no personal PAT). A new package starts
   private; GHCR visibility is UI-only, so make the package public before
   deploying, or Enclava cannot pull it.
2. Dispatch **Build signed image for Enclava** on `main` and enable
   `publish_release`. Keep the workflow at this repository, path and ref:
   `enclava create --signer-subject` and the trust policy pin its Fulcio
   identity (`certificate_identity`, `workflow_ref`, `source_repository`).
   Moving or renaming the workflow invalidates both and needs a policy re-pin. Normal pushes and dispatches without opt-in still build
   without Nostr publication. The release job requires the build, cosign
   signature, provenance and SBOM steps to have succeeded.
3. After relay acknowledgement, the job summary prints the exact event ID and
   deploy image, alongside the listing build ID baked into that image. Use the
   build ID from the **build** job (not a subsequent retry’s attempt number).
   See [provider discovery](discovery.md) for the deployment config. Download Actions artifact `enclava-release-event` to obtain
   `enclava-release-event.json`. Hand that public file to deployment tooling:
   deploy **that digest** and reference **that event ID** in the node listing.
   No release secret goes to the node. Retain the file beyond Actions artifact
   retention if you need a durable copy.

A failed or missing relay acknowledgement fails the publish job (it does not
roll back the GHCR image). The artifact is saved **before** the relay write:
retry with the same signed JSON to preserve the ID. With `nostr-sdk==0.45.1`
installed and the public file in the current directory, run
`python /path/to/scripts/enclava_release.py publish` with only
`ENCLAVA_RELEASE_PUBKEY` and `ENCLAVA_RELEASE_RELAY` set. Re-signing at a
later time produces a different event, not a retry of the original.
