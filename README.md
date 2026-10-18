# mycelium-classifier

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg?style=for-the-badge" alt="Apache 2.0 License"></a>
  <img src="https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white&style=for-the-badge">
</p>

<div align="center">
  <em>Flags agent failures in a <a href="https://github.com/mycelium-io/mycelium">Mycelium</a> room as they happen, with a short note in the chat that never blocks anyone.</em>
</div>

---

## The Problem

When several agents work in one room, some of their mistakes look like ordinary messages. An agent says a change was approved when nobody approved it. It says a task is done and tested when nothing shows it was. It makes a decision that was someone else's to make, or it drops something it was asked to do. A person reading every message would catch these. Nobody reads every message.

## What This Does

This project is a spike ([mycelium#1288](https://github.com/mycelium-io/mycelium/issues/1288)) to find out whether a small model can catch those mistakes well enough to be useful. It reads each message in a room and, when it sees one of a few known failures, posts a short note in the chat. The note never blocks a write and never wakes anyone.

It splits the problem in two:

1. **Is there a claim?** A small model tags what a message asserts: that something was approved, that it is done or verified, that a decision was taken, or that it is acting on some fact.
2. **Is the claim true?** A plain check compares the claim with what the room already knows: approvals in the thread, rows on the board, pull request state, and whose turn it is.

The first version looks for four failures:

| | Failure | What it looks like |
|---|---|---|
| C1 | Ungranted approval | "Approved, merging" with no approval in the thread |
| C2 | Unsupported completion | "Done and tested" with nothing that shows it |
| C3 | Decision taken | An agent settles a question that was someone else's |
| C4 | Dropped directive | An agent is asked for something and never answers |

C4 is a rule (a mention with no reply within a set time), not a model.

## How It Is Built

Simple methods come first, and a model is only trained where they fall short:

1. Rules.
2. A prompted one-shot model.
3. Embeddings with logistic regression.
4. Fine-tuning, only for a class with about 50 human-checked examples.

If fewer than two classes are worth training, the spike ends there, and that is a useful answer too.

## Data

**No room data is ever committed here.** Transcripts, labels and evaluation sets stay on the machine that made them, outside this repository, and `.gitignore` keeps them out. Before anything leaves a machine, emails, tokens and links other than GitHub ones are removed. Messages from agents are labeled; messages from people are used only as context.

This repository holds the code and the documentation only.

## Running the harvest

The harvest collects a hub's room histories into a dataset to label. It needs Python 3.12 and the `mycelium` CLI (3.0.43 or newer), and runs on any machine that can reach the hub:

```bash
PYTHONPATH=src python3 -m mycelium_classifier.harvest
```

For each room on the hub it runs `mycelium room export` twice, once as written and once with `--redact`. Each export folder keeps its own cursor, so a run fetches only what is new. It also reads the pull requests of the repository the rooms work on, with `gh`. It writes into `~/Documents/mycelium-classifier`, outside this repository:

| | |
|---|---|
| `exports/<room>/`, `exports-redacted/<room>/` | the two exports, as `room export` writes them |
| `raw/<room>.jsonl` | one line per message: who sent it and whether they are a person, an agent, an engine or the system; the task thread it is in; its type; its mentions; its text; and whether it is an agent's message to label |
| `raw/board-<room>.json` | every task, with its status, who it is for and who holds it |
| `raw/prs.json` | pull request state and reviews |
| `redacted/` | the same, with emails, tokens, links other than GitHub's and home-folder names replaced |
| `stats.json` | counts per room, and how many messages each rough rule would put in front of a labeler |
| `tally.jsonl` | one line per run, to see how fast each class grows |

Options: `--data` (or `MYCELIUM_CLASSIFIER_DATA`) for where the dataset goes, `--hub` for a hub other than the one the CLI is set up for, and `--repo` for the repository whose pull requests to read.

## Status

Early. Progress is reported on [mycelium#1288](https://github.com/mycelium-io/mycelium/issues/1288).

## Contributing

Issues and pull requests are welcome. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and report security problems as described in [SECURITY.md](SECURITY.md).

## License

[Apache 2.0](LICENSE)
