## What this changes

<!-- Which work package or issue, and what was built. -->

## How it was verified

<!-- Commands run and their results. For an acceptance criterion, show the evidence. -->

## Deviations from the concept and work packages

<!-- None, or list each one. A change to a contract (names in concept sections 4.1, 4.4, 5.1, 5.2)
     updates docs/concept.md in this pull request. -->

## Checklist

- [ ] `make check` passes locally
- [ ] The pipeline is green on the latest commit
- [ ] Tests assert behaviour; none of them can pass while the code is broken
- [ ] No secrets, no commented-out code, no `TODO` without an explanation
- [ ] New commands, variables and configuration keys are documented where a user would look
- [ ] Nothing outside this work package's paths changed, except its named shared files
