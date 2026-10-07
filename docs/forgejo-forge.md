# The forgejo forge

This page is for someone adding a repository hosted on Forgejo to the profile. It covers what the forge does, the three profile keys that select it, the token the Forgejo side must issue, and how to keep a Forgejo repository next to a GitHub one. Read the section on keys that are not wired yet before you edit any profile.

## What the forgejo forge does

The forgejo forge lands a task through a pull request on a Forgejo server, using the Forgejo REST API. It plays the same part that `github` plays for a repository on github.com.

It opens a pull request from the task's branch. Before it opens one, it looks for an open pull request on the same branch and reuses that number.

It reads the checks from the commit statuses on the pull request's head commit. A status is a check, so any CI that reports commit statuses to Forgejo gates the land. The forge waits for those statuses to settle and stops with the failing context names when one fails.

A head commit with no statuses at all is not a pass. The forge reports no checks and keeps waiting until the land's timeout. A repository with no CI that reports statuses to Forgejo therefore cannot land through this forge.

It asks Forgejo whether the pull request is mergeable. A pull request that Forgejo reports as not mergeable is not merged.

It lands the pull request with a squash merge. The default branch gains one commit for the task.

After the merge it asks Forgejo to delete the task's branch. A failed delete on the server does not fail the land. The result then carries a `branch not deleted` note and the land still counts as merged. The forge also deletes the task's local branch and updates the local default branch. When the task's branch is checked out, it switches the checkout to the default branch first.

## Keys that are not wired yet

Do not add `forgejo_base_url` or `forgejo_token_env` to a profile yet. The profile parser accepts only a fixed set of top-level keys. `forge` is in that set. The two Forgejo keys are not. A profile that carries either one raises a `ProfileError` on every command that reads the profile. The forgejo forge does not read them either. It contacts the host named by the repository's `origin` URL, and it reads the token from the fixed variable `FORGEJO_TOKEN`. Today the working setup is `forge: forgejo` and nothing else, with the token exported as `FORGEJO_TOKEN`. The rest of this page describes the intended contract, once the forge selection work makes the profile accept the keys.

## Profile keys

These are the keys the forgejo forge is built to read. They are defined in `agent_tools/forgejo_api.py`.

- `forge: forgejo` selects this forge.
- `forgejo_base_url` is the address of the Forgejo server. It must start with `http://` or `https://`. A trailing slash is removed.
- `forgejo_token_env` is the name of the environment variable that holds the API token. It is a name, never the token itself.

When the keys are wired, both `forgejo_base_url` and `forgejo_token_env` are required. Each must be a string with something in it. A missing, non-string or empty value is refused with a message that names the key.

Here is the intended shape for a server at `git.lan`. Do not paste it into a profile today, because the parser rejects the last two lines.

```yaml
forge: forgejo
forgejo_base_url: https://git.lan
forgejo_token_env: FORGEJO_TOKEN_EXAMPLE
```

The example shows the three keys and not where they sit in the profile. The forge selection work decides that placement. `FORGEJO_TOKEN_EXAMPLE` is a placeholder. Once the keys are wired, any variable name works, and you export it before you run `cox runs land`.

## The token

Create the token in the Forgejo user settings, under applications. Forgejo groups its token scopes by area, and pull requests fall under the repository area. Select these:

- repository read and write, which is `read:repository` and `write:repository` in Forgejo's scope list, to read the repository, read commit statuses and delete the branch
- pull request read and write, which the repository scopes above already grant, to list, open and merge pull requests

The token value is read from the environment. When the keys are wired, the variable is the one `forgejo_token_env` names, and requests go to `forgejo_base_url`. Until then, the variable is `FORGEJO_TOKEN` and requests go to the `origin` host. The token is not stored in the profile. It is sent only in the `Authorization` header of requests to the Forgejo server.

The token is never logged. Every error message that could carry it passes through a redaction step that replaces it with `***`, including the escaped forms that an HTTP library may quote. When the variable is missing or empty, the error names the variable and says nothing about a value.

## Choosing a forge per repository

Today `forge` is one value for the whole profile. To land one repository through Forgejo and another through GitHub, keep two profile files and pick one per command with `--profile PATH`. The Forgejo file holds `forge: forgejo`, and the token is exported as `FORGEJO_TOKEN`. The GitHub file holds `forge: github`.

The forge selection work is planned to let a repository's own profile entry choose `forge: forgejo`, so that one profile can hold a GitHub repository and a Forgejo repository. That is not built yet. Until it lands, use the two-file setup above and do not rely on any entry layout.
