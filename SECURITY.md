# Security Policy

## Supported versions

Only the most recent commit on `main` receives fixes. CHARON does not
currently maintain parallel supported release lines; if you are running a
pinned older release, upgrade to current `main` before reporting an issue so
the report is against supported code.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability.

Report it as a **confidential issue** on the canonical GitLab project:
<https://gitlab.com/ncz-os/charon/-/issues/new> — tick **"This issue is
confidential"** before you submit. Confidential issues are visible only to
project maintainers.

Please include:

- a description of the issue
- impact assessment
- reproduction steps
- the CHARON version/commit and which adapter/backend is involved
- any suggested remediation

Expect an acknowledgement within a week. There is no bug bounty.

## Secrets policy

- Never commit `.env` files or live credentials.
- Store provider/backend credentials outside the repository.
- Keep infrastructure-specific detail — hostnames, private addresses,
  internal topology — out of the repository entirely, including docs.
