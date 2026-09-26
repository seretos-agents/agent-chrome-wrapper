#!/usr/bin/env bash
# marketplace-payload.sh
#
# Builds the `plugin-release` `repository_dispatch` client_payload JSON,
# shared by release.yml's "Dispatch to agent-marketplace" step and
# dispatch.yml's own step of the same name (ticket #44 -- replaces two
# near-identical Python heredocs with one script).
#
# Required env: NAME DESC VERSION TAG REPO PAYLOAD_FILE
# Optional env: CHANGELOG_FILE -- when unset, missing, or empty/whitespace-
#   only, the "changelog" key is omitted from client_payload and a
#   `::warning::` is printed.
#
# The changelog body's exact bytes matter (this script exists to guarantee
# byte-for-byte fidelity, including a trailing "\r\n"), so it is never
# handed to jq as plain text via --rawfile or stdin: at least one real-world
# jq build on Windows silently translates "\r\n" -> "\n" when reading a file
# or stdin that way (a text-mode `fopen`), which would corrupt exactly the
# bytes this script must preserve -- and passing the raw text as a
# command-line `--arg` instead hits the OS argument-length ceiling well
# before a changelog reaches the 30000-byte truncation limit ("Argument
# list too long"). Base64 avoids both: it is pure ASCII with no embedded
# "\n"/"\r" for a text-mode reader to mistranslate, and it is delivered via
# a file (`--rawfile`, no argv-size limit), then decoded back to the exact
# original bytes inside jq with `@base64d`. Exactly one trailing "\n"
# (gh's own --jq-rendering newline) is stripped from the decoded body, and
# an oversized body is truncated to a UTF-8 *byte* budget (not a character
# budget) without ever cutting a multi-byte codepoint in half. Output is
# raw UTF-8 (jq's default; `-a`/--ascii-output is never passed), never
# \uXXXX-escaped.
set -euo pipefail

: "${NAME:?NAME is required}"
: "${DESC:?DESC is required}"
: "${VERSION:?VERSION is required}"
: "${TAG:?TAG is required}"
: "${REPO:?REPO is required}"
: "${PAYLOAD_FILE:?PAYLOAD_FILE is required}"

TRUNCATE_LIMIT=30000

CHANGELOG_B64_FILE="$(mktemp)"
cleanup() {
  rm -f "$CHANGELOG_B64_FILE"
}
trap cleanup EXIT

if [ -n "${CHANGELOG_FILE:-}" ] && [ -f "$CHANGELOG_FILE" ]; then
  base64 -w0 "$CHANGELOG_FILE" >"$CHANGELOG_B64_FILE"
fi
# else: leave $CHANGELOG_B64_FILE empty -- an absent/unset CHANGELOG_FILE
# means "no changelog", exactly like an empty one.

jq -n -c \
  --rawfile b64 "$CHANGELOG_B64_FILE" \
  --arg name "$NAME" \
  --arg desc "$DESC" \
  --arg version "$VERSION" \
  --arg tag "$TAG" \
  --arg repo "$REPO" \
  --argjson limit "$TRUNCATE_LIMIT" \
  '
  def utf8_byte_len:
    if . < 128 then 1
    elif . < 2048 then 2
    elif . < 65536 then 3
    else 4
    end;

  def utf8_len:
    (explode | map(utf8_byte_len) | add) // 0;

  # Keeps the longest whole-codepoint prefix of the exploded string whose
  # UTF-8 byte length is <= $limit -- never slices a multi-byte codepoint in
  # half, so no replacement-character cleanup is ever needed.
  def truncate_utf8($limit):
    explode
    | reduce .[] as $cp
        ({bytes: 0, out: []};
          ($cp | utf8_byte_len) as $len
          | if (.bytes + $len) > $limit then .
            else {bytes: (.bytes + $len), out: (.out + [$cp])}
            end
        )
    | .out
    | implode;

  ($b64 | @base64d) as $raw
  | ($raw | if (.[-1:] == "\n") then .[0:-1] else . end) as $body
  | {
      name: $name,
      description: $desc,
      repo: $repo,
      category: "mcp",
      version: $version,
      ref: $tag,
      icon: "https://raw.githubusercontent.com/\($repo)/\($tag)/assets/icon.png",
      description_url: "https://raw.githubusercontent.com/\($repo)/\($tag)/description.md",
    } as $base
  | if ($body | test("\\S")) then
      (if ($body | utf8_len) > $limit then
         ($body | truncate_utf8($limit))
         + "\n\n_… truncated. Full release notes: https://github.com/\($repo)/releases/tag/\($tag)_"
       else
         $body
       end) as $changelog
      | $base + {changelog: $changelog}
    else
      $base
    end
  | {event_type: "plugin-release", client_payload: .}
  ' >"$PAYLOAD_FILE"

# The same jq build whose text-mode file reading motivated the base64 detour
# above also affects its own stdout on Windows: it can emit "\r\n" for the
# single trailing newline it appends after the compact JSON, even though
# nothing inside the JSON itself is a raw, unescaped CR byte (jq's compact
# output always escapes a real "\r" inside a string as the two characters
# "\r", never a literal CR byte -- see json.org). Stripping every literal CR
# byte from the finished file is therefore safe and restores byte-exact
# output (curl -d @file would otherwise still strip it, but only right
# before POSTing -- the file on disk must already be correct).
tr -d '\r' <"$PAYLOAD_FILE" >"${PAYLOAD_FILE}.tmp"
mv "${PAYLOAD_FILE}.tmp" "$PAYLOAD_FILE"

if ! jq -e '.client_payload | has("changelog")' "$PAYLOAD_FILE" >/dev/null; then
  echo "::warning::No release body found for $TAG; dispatching without a changelog."
fi
