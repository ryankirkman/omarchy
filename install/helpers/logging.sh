omarchy_log_to_stdout() {
  [[ ${OMARCHY_LOG_TO_STDOUT:-} == "1" || -z ${OMARCHY_INSTALL_LOG_FILE:-} ]]
}

omarchy_log_line() {
  if omarchy_log_to_stdout; then
    echo "$1"
  else
    echo "$1" >>"$OMARCHY_INSTALL_LOG_FILE"
  fi
}

start_install_log() {
  if ! omarchy_log_to_stdout; then
    mkdir -p "$(dirname "$OMARCHY_INSTALL_LOG_FILE")"
    touch "$OMARCHY_INSTALL_LOG_FILE"
    chmod 666 "$OMARCHY_INSTALL_LOG_FILE" 2>/dev/null || true
  fi

  if [[ -z ${OMARCHY_START_TIME:-} ]]; then
    printf -v OMARCHY_START_TIME '%(%Y-%m-%d %H:%M:%S)T' -1
  fi
  export OMARCHY_START_TIME
  export OMARCHY_START_EPOCH="${OMARCHY_START_EPOCH:-$EPOCHSECONDS}"

  omarchy_log_line "=== Omarchy Setup Started: $OMARCHY_START_TIME ==="
}

stop_install_log() {
  local end_time end_epoch duration mins secs
  printf -v end_time '%(%Y-%m-%d %H:%M:%S)T' -1
  end_epoch=$EPOCHSECONDS

  omarchy_log_line "=== Omarchy Setup Completed: $end_time ==="

  if [[ -n ${OMARCHY_START_EPOCH:-} ]]; then
    duration=$((end_epoch - OMARCHY_START_EPOCH))
    mins=$((duration / 60))
    secs=$((duration % 60))
    omarchy_log_line "Omarchy setup: ${mins}m ${secs}s"
  fi
}

run_logged() {
  local script="$1"
  local exit_code timestamp errexit_was_set=0

  # Bash's clock formatter avoids two date processes for every setup leaf.
  printf -v timestamp '%(%Y-%m-%d %H:%M:%S)T' -1
  omarchy_log_line "[$timestamp] Starting: $script"

  case $- in
    *e*)
      errexit_was_set=1
      set +e
      ;;
  esac

  local runner=(bash -eE)
  if [[ ${OMARCHY_INSTALL_DEBUG:-} == "1" ]]; then
    runner=(bash -x -eE)
  fi

  if omarchy_log_to_stdout; then
    PS4='+ ${BASH_SOURCE[0]##*/}:${LINENO}:${FUNCNAME[0]:-main}: ' \
      "${runner[@]}" -c 'source "$1"' bash "$script" </dev/null 2>&1
  else
    PS4='+ ${BASH_SOURCE[0]##*/}:${LINENO}:${FUNCNAME[0]:-main}: ' \
      "${runner[@]}" -c 'source "$1"' bash "$script" </dev/null >>"$OMARCHY_INSTALL_LOG_FILE" 2>&1
  fi

  exit_code=$?
  (( errexit_was_set )) && set -e

  printf -v timestamp '%(%Y-%m-%d %H:%M:%S)T' -1
  if (( exit_code == 0 )); then
    omarchy_log_line "[$timestamp] Completed: $script"
  else
    omarchy_log_line "[$timestamp] Failed: $script (exit code: $exit_code)"
  fi

  return $exit_code
}
