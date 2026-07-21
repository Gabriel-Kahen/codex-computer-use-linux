# Security Policy

Codex Computer Use on Linux is an independently maintained fork of OpenAI
Codex. This policy covers vulnerabilities in this repository's modifications,
release artifacts, plugin packaging, Linux computer-use engine, and desktop
integrations. It is not the OpenAI security policy and does not enroll reports
in OpenAI's bug-bounty program.

## Supported versions

Security fixes are made on a best-effort basis for the current `main` branch
and the latest release of this fork. Older releases might not receive
backports. Users should reproduce an issue on the latest available version when
it is safe to do so.

## Reporting a vulnerability

GitHub private vulnerability reporting is not currently enabled for this
repository. Do not disclose a suspected vulnerability in a public pull request
or other public channel. Email the maintainer at
[`gabekahen@gmail.com`](mailto:gabekahen@gmail.com) with the subject
`codex-computer-use-linux security report` and include:

- the affected component and version or commit;
- steps to reproduce or a proof of concept;
- the expected security impact;
- relevant logs with credentials and personal data removed; and
- any suggested mitigation or disclosure deadline.

Reports are reviewed on a best-effort basis. The maintainer will try to
acknowledge a report, validate it, coordinate a fix, and agree on disclosure,
but this project does not promise a particular response time or a reward.

Do not include live credentials, access tokens, private user data, or other
secrets in the initial report. The maintainer can coordinate a safer transfer
method if additional sensitive artifacts are needed.

## Upstream vulnerabilities

If a vulnerability is present in unmodified OpenAI Codex and is unrelated to
this fork's changes, report it through the
[OpenAI Codex security policy](https://github.com/openai/codex/security/policy).
If it also affects this fork, send a private report here as well so mitigation
can be tracked for this repository's users.

## Safe operation

Computer use can expose private on-screen and accessibility content and can
perform actions in other applications. Review the
[safety and limitations](README.md#safety-and-limitations) and the selected
desktop backend's safety section before enabling it. Keep approvals enabled for
consequential actions and do not use these integrations to bypass application
security controls or authentication.
