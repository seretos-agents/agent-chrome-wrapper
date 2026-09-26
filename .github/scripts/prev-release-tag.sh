#!/usr/bin/env bash
# prev-release-tag.sh <plugin> <version>
#
# Prints the greatest existing `<plugin>--v<semver>` tag whose version is
# strictly lower than <version>, using pure-bash SemVer 2.0 SS11 precedence
# ordering (NOT `sort -V`, which puts 1.0.0 below 1.0.0-rc.1). Prints nothing
# (exit 0) when there is no such tag (first release). Exits 2 if <version>
# itself is not valid strict SemVer (no leading zeros, no build metadata).
#
# Only tags matching the literal prefix "<plugin>--v" are considered -- never
# `src/*` markers, and never another plugin's tags -- via a bash `case`
# prefix check (not git's own tag-name globbing, whose slash-matching
# semantics are not something this script wants to depend on).
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "Usage: prev-release-tag.sh <plugin> <version>" >&2
  exit 2
fi

PLUGIN="$1"
VERSION="$2"

# Strict SemVer 2.0 grammar, minus build metadata (deliberately not
# supported here) and without leading zeros in any numeric identifier.
SEMVER_RE='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(\.(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?$'

is_valid_semver() {
  [[ "$1" =~ $SEMVER_RE ]]
}

if ! is_valid_semver "$VERSION"; then
  echo "::error::Version '$VERSION' is not valid SemVer 2.0 (MAJOR.MINOR.PATCH[-PRERELEASE], no leading zeros, no build metadata)." >&2
  exit 2
fi

# Splits "$1" into four space-separated fields: major minor patch prerelease
# (prerelease is empty when there is none).
split_version() {
  local v="$1" core pre="" major minor patch
  core="${v%%-*}"
  if [[ "$v" == *-* ]]; then
    pre="${v#*-}"
  fi
  IFS='.' read -r major minor patch <<<"$core"
  printf '%s %s %s %s\n' "$major" "$minor" "$patch" "$pre"
}

# Compares two SemVer dot-separated prerelease IDENTIFIERS per SS11 rule 4:
# numeric identifiers compare numerically; alphanumeric compare lexically
# (ASCII); a numeric identifier always has lower precedence than an
# alphanumeric one. Echoes lt|eq|gt.
compare_identifier() {
  local a="$1" b="$2" a_num=0 b_num=0
  [[ "$a" =~ ^[0-9]+$ ]] && a_num=1
  [[ "$b" =~ ^[0-9]+$ ]] && b_num=1
  if [ "$a_num" -eq 1 ] && [ "$b_num" -eq 1 ]; then
    if [ "$a" -lt "$b" ]; then echo lt
    elif [ "$a" -gt "$b" ]; then echo gt
    else echo eq
    fi
  elif [ "$a_num" -eq 1 ]; then
    echo lt
  elif [ "$b_num" -eq 1 ]; then
    echo gt
  else
    if [[ "$a" < "$b" ]]; then echo lt
    elif [[ "$a" > "$b" ]]; then echo gt
    else echo eq
    fi
  fi
}

# Compares two full prerelease strings (dot-separated identifier lists, ""
# meaning "no prerelease"). SS11 rule 2: no prerelease outranks any
# prerelease. Rule 3: compare identifiers left to right; a longer set of
# equal-so-far fields has higher precedence. Echoes lt|eq|gt.
compare_prerelease() {
  local pre_a="$1" pre_b="$2"
  if [ -z "$pre_a" ] && [ -z "$pre_b" ]; then echo eq; return; fi
  if [ -z "$pre_a" ]; then echo gt; return; fi
  if [ -z "$pre_b" ]; then echo lt; return; fi

  local -a a_parts b_parts
  IFS='.' read -r -a a_parts <<<"$pre_a"
  IFS='.' read -r -a b_parts <<<"$pre_b"
  local len_a=${#a_parts[@]} len_b=${#b_parts[@]}
  local min_len=$len_a
  [ "$len_b" -lt "$min_len" ] && min_len=$len_b

  local i=0 cmp
  while [ "$i" -lt "$min_len" ]; do
    cmp=$(compare_identifier "${a_parts[$i]}" "${b_parts[$i]}")
    if [ "$cmp" != "eq" ]; then
      echo "$cmp"
      return
    fi
    i=$((i + 1))
  done

  if [ "$len_a" -lt "$len_b" ]; then echo lt
  elif [ "$len_a" -gt "$len_b" ]; then echo gt
  else echo eq
  fi
}

# Compares two full, validated SemVer strings. Echoes lt|eq|gt.
semver_compare() {
  local a="$1" b="$2"
  local a_major a_minor a_patch a_pre b_major b_minor b_patch b_pre
  read -r a_major a_minor a_patch a_pre <<<"$(split_version "$a")"
  read -r b_major b_minor b_patch b_pre <<<"$(split_version "$b")"

  if [ "$a_major" -ne "$b_major" ]; then
    if [ "$a_major" -lt "$b_major" ]; then echo lt; else echo gt; fi
    return
  fi
  if [ "$a_minor" -ne "$b_minor" ]; then
    if [ "$a_minor" -lt "$b_minor" ]; then echo lt; else echo gt; fi
    return
  fi
  if [ "$a_patch" -ne "$b_patch" ]; then
    if [ "$a_patch" -lt "$b_patch" ]; then echo lt; else echo gt; fi
    return
  fi
  compare_prerelease "$a_pre" "$b_pre"
}

best=""
while IFS= read -r tag; do
  [ -z "$tag" ] && continue
  case "$tag" in
    "${PLUGIN}--v"*) ;;
    *) continue ;;
  esac
  candidate="${tag#"${PLUGIN}--v"}"
  is_valid_semver "$candidate" || continue

  cmp=$(semver_compare "$candidate" "$VERSION")
  [ "$cmp" = "lt" ] || continue

  if [ -z "$best" ]; then
    best="$candidate"
  else
    cmp2=$(semver_compare "$candidate" "$best")
    [ "$cmp2" = "gt" ] && best="$candidate"
  fi
done < <(git tag -l)

if [ -n "$best" ]; then
  echo "${PLUGIN}--v${best}"
fi
exit 0
