# Contributing

## Public content

Write documentation, command examples, code comments, commit messages and GitHub issues in English. Use the actual executable in public examples, with every required dependency documented. Local agent wrappers, private shell aliases and workstation-specific setup are not product requirements.

Check the complete publication candidate, including README, operations/testing guides, skill instructions, CI configuration and release contents. Examples must work on a clean supported host using the documented installation.

Run the publication check before committing:

```bash
python3 scripts/check-publication.py
# After staging, validate the exact contents that will be committed:
python3 scripts/check-publication.py --staged
```

The check rejects known internal command wrappers in public documents, Korean text accidentally carried into English artifacts, personal absolute paths, common credential patterns and raw evidence files included in Git. It is a regression guard, not proof of English quality or a complete secret audit. Review the diff and executable examples as well.

## Verification and updates

Run the applicable regression checks from [TESTING.md](docs/TESTING.md). Update [TODO.md](docs/TODO.md), [STATUS.md](docs/STATUS.md) and the curated public evidence summary when results change. Distinguish protocol acceptance, native consumption, actual user-visible behavior and completed model work.

Keep original reports, conversation transcripts, credentials and local service data outside Git. Publish reviewed summaries with test dates, versions, scope, failures and remaining limitations. Publishing source is separate from publishing a release or choosing a license.
