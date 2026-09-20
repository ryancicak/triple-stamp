#!/bin/sh
# Idempotent first-run bootstrap for Triple-stamp on macOS.
#
# This file is sourced by ../triple-stamp. It installs only into the current
# user's home directory, never elevates privileges, and leaves unrelated Omnigent
# installations untouched.

triple_stamp_bootstrap_log() {
  printf '%s\n' "triple-stamp setup: $*" >&2
}

triple_stamp_bootstrap_die() {
  triple_stamp_bootstrap_log "$*"
  return 78
}

triple_stamp_find_uv() {
  for candidate in \
    "${TRIPLE_STAMP_UV_BIN:-}" \
    "$(command -v uv 2>/dev/null || true)" \
    "$HOME/.local/bin/uv" \
    "/opt/homebrew/bin/uv" \
    "/usr/local/bin/uv"
  do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

triple_stamp_install_uv() {
  if [ "${TRIPLE_STAMP_BOOTSTRAP_OFFLINE:-0}" = "1" ]; then
    triple_stamp_bootstrap_die \
      "uv is missing and offline mode is enabled; install uv, then rerun ./triple-stamp"
    return $?
  fi
  command -v curl >/dev/null 2>&1 || {
    triple_stamp_bootstrap_die "macOS curl is unavailable; cannot install uv"
    return $?
  }
  install_dir="$HOME/.local/bin"
  mkdir -p "$install_dir"
  installer=$(mktemp "${TMPDIR:-/tmp}/triple-stamp-uv.XXXXXX") || return 78
  triple_stamp_bootstrap_log "installing uv for this user..."
  if ! curl --fail --location --silent --show-error \
    --retry 3 --retry-delay 1 --connect-timeout 10 --max-time 60 \
    --proto '=https' --tlsv1.2 \
    https://astral.sh/uv/install.sh --output "$installer"
  then
    rm -f "$installer"
    triple_stamp_bootstrap_die "could not download the official uv installer"
    return $?
  fi
  if ! triple_stamp_run_timed 300 \
    /usr/bin/env UV_UNMANAGED_INSTALL="$install_dir" \
    /bin/sh "$installer" >&2
  then
    rm -f "$installer"
    triple_stamp_bootstrap_die "the official uv installer failed"
    return $?
  fi
  rm -f "$installer"
  [ -x "$install_dir/uv" ] || {
    triple_stamp_bootstrap_die "uv installer completed without $install_dir/uv"
    return $?
  }
  printf '%s\n' "$install_dir/uv"
}

triple_stamp_run_timed() {
  seconds=$1
  shift
  /usr/bin/perl -e 'alarm shift; exec @ARGV or exit 127' "$seconds" "$@"
}

triple_stamp_run_retry() {
  attempts=$1
  delay=$2
  timeout=$3
  shift 3
  current=1
  while :; do
    triple_stamp_run_timed "$timeout" "$@" && return 0
    status=$?
    if [ "$current" -ge "$attempts" ]; then
      return "$status"
    fi
    triple_stamp_bootstrap_log \
      "download attempt $current failed; retrying in ${delay}s..."
    sleep "$delay"
    current=$((current + 1))
  done
}

triple_stamp_runtime_supported() {
  candidate=$1
  [ -x "$candidate" ] || return 1
  output=$(
    "$candidate" -I \
      "$TRIPLE_STAMP_ROOT/.omnigent/runtime-python/triple_stamp_omnigent_compat.py" \
      --probe 2>/dev/null
  ) || return 1
  printf '%s' "$output" | "$candidate" -I -c \
    'import json,sys; d=json.load(sys.stdin); v=str(d.get("omnigent_version","")); raise SystemExit(not (d.get("supported") is True and d.get("surfaces_ok") is True and (v.startswith("0.12.") or v.startswith("0.14."))))' \
    >/dev/null 2>&1
}

triple_stamp_sdk_pinned() {
  "$1" -I -c \
    'import importlib.metadata as m; raise SystemExit(m.version("claude-agent-sdk") != "0.2.152")' \
    >/dev/null 2>&1
}

triple_stamp_runtime_entrypoint_supported() {
  runtime=$1
  entrypoint=$(dirname "$runtime")/omnigent
  [ -x "$entrypoint" ] || return 1
  triple_stamp_run_timed 30 "$entrypoint" --help >/dev/null 2>&1
}

triple_stamp_runtime_candidates() {
  # Only this clone's private runtime is writable. Never install Triple-stamp's
  # clone-relative plugin into a user's shared Omnigent tool environment.
  printf '%s\n' "$TRIPLE_STAMP_MANAGED_RUNTIME/bin/python"
}

triple_stamp_find_supported_runtime() {
  seen=
  triple_stamp_runtime_candidates | while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    case "
$seen
" in
      *"
$candidate
"*) continue ;;
    esac
    seen="${seen}
$candidate"
    if triple_stamp_runtime_supported "$candidate" &&
      triple_stamp_sdk_pinned "$candidate" &&
      triple_stamp_runtime_entrypoint_supported "$candidate"
    then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
}

triple_stamp_pin_sdk() {
  runtime=$1
  if triple_stamp_sdk_pinned "$runtime"; then
    return 0
  fi
  triple_stamp_bootstrap_log "repairing claude-agent-sdk to 0.2.152..."
  triple_stamp_run_retry 3 5 600 \
    "$TRIPLE_STAMP_UV" pip install --python "$runtime" \
    'claude-agent-sdk==0.2.152' >/dev/null || {
      triple_stamp_bootstrap_die "could not pin claude-agent-sdk==0.2.152"
      return $?
    }
}

triple_stamp_install_runtime() {
  version=${TRIPLE_STAMP_BOOTSTRAP_OMNIGENT_VERSION:-0.14.0}
  case "$version" in
    0.12.*|0.14.*) ;;
    *)
      triple_stamp_bootstrap_die \
        "TRIPLE_STAMP_BOOTSTRAP_OMNIGENT_VERSION must be 0.12.x or 0.14.x"
      return $?
      ;;
  esac
  if [ "${TRIPLE_STAMP_BOOTSTRAP_OFFLINE:-0}" = "1" ]; then
    triple_stamp_bootstrap_die \
      "no supported Omnigent runtime is installed and offline mode is enabled"
    return $?
  fi

  parent=$(dirname "$TRIPLE_STAMP_MANAGED_RUNTIME")
  mkdir -p "$parent"
  staging="$parent/runtime.build.$$"
  backup="$parent/runtime.old.$$"
  new_link="$parent/runtime.link.$$"
  rm -rf "$staging" "$backup" "$new_link"
  triple_stamp_bootstrap_log \
    "installing a private Omnigent $version runtime (Python 3.13)..."
  attempt=1
  while :; do
    rm -rf "$staging"
    if triple_stamp_run_timed 300 \
      "$TRIPLE_STAMP_UV" venv --python 3.13 "$staging"
    then
      break
    else
      status=$?
    fi
    if [ "$attempt" -ge 3 ]; then
      rm -rf "$staging"
      triple_stamp_bootstrap_die \
        "could not provision Python 3.13 (last exit $status)"
      return $?
    fi
    triple_stamp_bootstrap_log \
      "Python provision attempt $attempt failed; retrying in 5s..."
    sleep 5
    attempt=$((attempt + 1))
  done
  if [ ! -x "$staging/bin/python" ]; then
    rm -rf "$staging"
    triple_stamp_bootstrap_die "could not provision Python 3.13"
    return $?
  fi
  if ! triple_stamp_run_retry 3 5 600 \
    "$TRIPLE_STAMP_UV" pip install --python "$staging/bin/python" \
    "omnigent==$version" 'claude-agent-sdk==0.2.152'
  then
    rm -rf "$staging"
    triple_stamp_bootstrap_die "could not install Omnigent $version"
    return $?
  fi
  if ! triple_stamp_runtime_supported "$staging/bin/python"; then
    rm -rf "$staging"
    triple_stamp_bootstrap_die \
      "the installed Omnigent $version runtime failed its compatibility probe"
    return $?
  fi
  if ! triple_stamp_sdk_pinned "$staging/bin/python" ||
    ! triple_stamp_runtime_entrypoint_supported "$staging/bin/python"
  then
    rm -rf "$staging"
    triple_stamp_bootstrap_die \
      "the installed Omnigent $version runtime has an unusable entrypoint"
    return $?
  fi
  ln -s "$(basename "$staging")" "$new_link" || {
    rm -rf "$staging"
    triple_stamp_bootstrap_die "could not prepare the managed runtime link"
    return $?
  }
  if [ -e "$TRIPLE_STAMP_MANAGED_RUNTIME" ] ||
    [ -L "$TRIPLE_STAMP_MANAGED_RUNTIME" ]
  then
    mv "$TRIPLE_STAMP_MANAGED_RUNTIME" "$backup" || {
      rm -rf "$staging" "$new_link"
      triple_stamp_bootstrap_die \
        "could not stage the previous managed runtime for replacement"
      return $?
    }
  fi
  if ! mv "$new_link" "$TRIPLE_STAMP_MANAGED_RUNTIME"; then
    [ ! -e "$backup" ] || mv "$backup" "$TRIPLE_STAMP_MANAGED_RUNTIME"
    rm -rf "$staging" "$new_link"
    triple_stamp_bootstrap_die \
      "could not activate the new runtime; the previous runtime was restored"
    return $?
  fi
  if ! triple_stamp_runtime_entrypoint_supported \
    "$TRIPLE_STAMP_MANAGED_RUNTIME/bin/python"
  then
    rm -f "$TRIPLE_STAMP_MANAGED_RUNTIME"
    [ ! -e "$backup" ] || mv "$backup" "$TRIPLE_STAMP_MANAGED_RUNTIME"
    rm -rf "$staging"
    triple_stamp_bootstrap_die \
      "the activated Omnigent runtime entrypoint failed its final probe"
    return $?
  fi
  rm -rf "$backup"
  printf '%s\n' "$TRIPLE_STAMP_MANAGED_RUNTIME/bin/python"
}

triple_stamp_recover_runtime_swap() {
  parent=$(dirname "$TRIPLE_STAMP_MANAGED_RUNTIME")
  mkdir -p "$parent"
  for trash in "$parent"/runtime.trash.*; do
    [ -e "$trash" ] || continue
    rm -rf "$trash" 2>/dev/null || true
  done
  for pending_link in "$parent"/runtime.link.*; do
    [ -L "$pending_link" ] || continue
    rm -f "$pending_link"
  done
  for backup in "$parent"/runtime.old.*; do
    [ -e "$backup" ] || [ -L "$backup" ] || continue
    if [ ! -e "$TRIPLE_STAMP_MANAGED_RUNTIME" ] &&
      [ ! -L "$TRIPLE_STAMP_MANAGED_RUNTIME" ] &&
      triple_stamp_runtime_supported "$backup/bin/python" &&
      triple_stamp_sdk_pinned "$backup/bin/python" &&
      triple_stamp_runtime_entrypoint_supported "$backup/bin/python"
    then
      triple_stamp_bootstrap_log "recovering the previous managed runtime..."
      mv "$backup" "$TRIPLE_STAMP_MANAGED_RUNTIME" || {
        triple_stamp_bootstrap_die \
          "could not recover the previous managed runtime from $backup"
        return $?
      }
    else
      backup_target=
      if [ -L "$backup" ]; then
        backup_target=$(readlink "$backup" || true)
      fi
      rm -rf "$backup"
      case "$backup_target" in
        runtime.build.*)
          old_build="$parent/$backup_target"
          trash="$parent/runtime.trash.$$.${backup_target#runtime.build.}"
          mv "$old_build" "$trash" 2>/dev/null || true
          rm -rf "$trash" 2>/dev/null || true
          ;;
      esac
    fi
  done
  active_target=
  preserved_targets="
"
  for runtime_link in "$parent"/*; do
    [ -L "$runtime_link" ] || continue
    linked_target=$(readlink "$runtime_link" || true)
    case "$linked_target" in
      runtime.build.*)
        preserved_targets="${preserved_targets}${linked_target}
"
        ;;
    esac
  done
  for build in "$parent"/runtime.build.*; do
    [ -e "$build" ] || continue
    build_name=$(basename "$build")
    case "$preserved_targets" in
      *"
$build_name
"*) continue ;;
    esac
    trash="$parent/runtime.trash.$$.${build##*.}"
    mv "$build" "$trash" 2>/dev/null || continue
    rm -rf "$trash" 2>/dev/null || true
  done
}

triple_stamp_root_identity() {
  printf '%s' "$1" |
    /usr/bin/cksum |
    /usr/bin/awk '{print $1 "-" $2}'
}

triple_stamp_acquire_bootstrap_lock() {
  lock=$1
  attempts=0
  while ! /usr/bin/shlock -f "$lock" -p "$$" 2>/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 120 ]; then
      holder=$(cat "$lock" 2>/dev/null || printf unknown)
      triple_stamp_bootstrap_die \
        "another first-run setup (PID $holder) is still active; finish its browser login or press Ctrl-C there"
      return $?
    fi
    sleep 1
  done
}

triple_stamp_bootstrap() {
  [ "$(uname -s)" = "Darwin" ] || {
    triple_stamp_bootstrap_die "Triple-stamp requires macOS"
    return $?
  }
  if [ "${TRIPLE_STAMP_BOOTSTRAP:-1}" = "0" ]; then
    return 0
  fi

  TRIPLE_STAMP_STATE_DIR=${TRIPLE_STAMP_STATE_DIR:-"$HOME/.local/share/triple-stamp"}
  managed_version=${TRIPLE_STAMP_BOOTSTRAP_OMNIGENT_VERSION:-0.14.0}
  case "$managed_version" in
    0.12.*|0.14.*) ;;
    *)
      triple_stamp_bootstrap_die \
        "TRIPLE_STAMP_BOOTSTRAP_OMNIGENT_VERSION must be 0.12.x or 0.14.x"
      return $?
      ;;
  esac
  root_identity=$(triple_stamp_root_identity "$TRIPLE_STAMP_ROOT")
  runtime_generation="clone-$root_identity/omnigent-$managed_version-sdk-0.2.152-py3.13"
  TRIPLE_STAMP_MANAGED_RUNTIME=${TRIPLE_STAMP_MANAGED_RUNTIME:-"$TRIPLE_STAMP_STATE_DIR/runtimes/$runtime_generation"}
  export TRIPLE_STAMP_STATE_DIR TRIPLE_STAMP_MANAGED_RUNTIME
  mkdir -p "$TRIPLE_STAMP_STATE_DIR"
  chmod 700 "$TRIPLE_STAMP_STATE_DIR"
  lock="$TRIPLE_STAMP_STATE_DIR/bootstrap.lock"
  triple_stamp_acquire_bootstrap_lock "$lock" || return $?
  trap 'rm -f "$lock"' EXIT
  trap 'rm -f "$lock"; exit 130' HUP INT TERM

  TRIPLE_STAMP_UV=$(triple_stamp_find_uv || true)
  if [ -z "$TRIPLE_STAMP_UV" ]; then
    TRIPLE_STAMP_UV=$(triple_stamp_install_uv) || return $?
  fi
  export TRIPLE_STAMP_UV
  PATH="$HOME/.local/bin:$(dirname "$TRIPLE_STAMP_UV"):$PATH"
  export PATH

  triple_stamp_recover_runtime_swap || return $?
  runtime=$(triple_stamp_find_supported_runtime || true)
  if [ -z "$runtime" ]; then
    runtime=$(triple_stamp_install_runtime) || return $?
  fi
  if ! triple_stamp_pin_sdk "$runtime"; then
    triple_stamp_bootstrap_log \
      "the existing runtime could not be repaired; using a private runtime instead"
    runtime=$(triple_stamp_install_runtime) || return $?
    triple_stamp_pin_sdk "$runtime" || return $?
  fi
  STABLE_OMNIGENT_PY=$runtime
  export STABLE_OMNIGENT_PY

  provider=${TRIPLE_STAMP_PROVIDER:-direct}
  case "${1:-}" in
    --help|-h) ;;
    *)
      if [ "$provider" = "databricks" ]; then
        TRIPLE_STAMP_BOOTSTRAP_SKIP_LOGIN=1 \
          "$STABLE_OMNIGENT_PY" -I \
          "$TRIPLE_STAMP_ROOT/.omnigent/outsider_preflight.py" \
          --install || return $?
      else
        "$STABLE_OMNIGENT_PY" -I \
          "$TRIPLE_STAMP_ROOT/.omnigent/outsider_preflight.py" \
          --install || return $?
      fi
      ;;
  esac

  rm -f "$lock"
  trap - EXIT
  trap - HUP INT TERM
  return 0
}
