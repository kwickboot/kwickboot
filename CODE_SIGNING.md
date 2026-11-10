# Windows code-signing policy

## Current status

KwickBoot's initial application for free open-source code signing through SignPath Foundation was not approved because
the new project does not yet have the public adoption signals required by the Foundation program. Windows releases
remain unsigned. Verify current downloads with the published SHA-256 checksums and detached release signature.

## Scope

The signing service will be used only for Windows artifacts built from this public repository:

- `KwickBootUSB.exe`, the KwickBoot USB Creator launcher
- future Windows installers or packages whose complete corresponding source is in this repository

The certificate will not be used for third-party binaries, private builds, unrelated projects, or artifacts modified
after the trusted build completes. A signed artifact must be traceable to a public Git commit and an automated build.

## Release controls

- A maintainer reviews each production signing request.
- Release builds originate from the public `kwickboot/kwickboot` repository.
- Signing credentials and private keys are never stored in the repository or build artifacts.
- SHA-256 checksums remain published independently of the Authenticode signature.
- A Windows signature is verified after signing and before publication.
- If a release is suspected of compromise, it is removed while the incident is investigated and the signing provider
  is notified when certificate revocation may be required.

If a trusted signing provider approves the project, this policy will be updated to identify the provider, publisher
name and active trusted-build configuration before signed releases are published.

## Privacy

The USB Creator does not require a KwickBoot cloud account and does not collect analytics. It writes only to the USB
device explicitly selected and confirmed by the operator. Network access is limited to downloading user-requested
KwickBoot release files and their verification material when they are not supplied locally.

