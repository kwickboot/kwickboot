# Contributing to KwickBoot

Thank you for helping improve KwickBoot. Bug reports, hardware compatibility
results, documentation fixes and focused code changes are all useful.

## Before opening an issue

- Search existing issues and discussions.
- Remove passwords, private IP addresses, hostnames and machine serial numbers.
- For boot problems, include firmware mode, Secure Boot state, NIC model and the
  last visible boot message.
- For disk or deployment problems, include the intended target-disk model, but
  redact its serial number.

Use a GitHub Discussion for setup questions and design ideas. Use an Issue for a
reproducible defect or a scoped feature request.

## Development workflow

1. Fork the repository and create a focused branch.
2. Keep unrelated formatting changes out of the patch.
3. Add or update tests when behavior changes.
4. Run the relevant self-tests and describe the environment used.
5. Open a pull request explaining the problem, the change and its safety impact.

Changes that can write, restore or wipe disks require especially clear target
validation and a test performed against disposable media or a virtual disk.

By contributing, you agree that your work is licensed under the repository's
GNU AGPL-3.0-or-later license.
