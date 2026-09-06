"""Move one full foreground logo effect into PR145's existing child window."""

import hashlib


PIN = "dbffaa6c65344d644627a023c28661e08382b8fa"
SOURCE_PATH = "configs/airootfs/usr/local/bin/omarchy-install-dashboard"
SOURCE_SHA256 = "4871faded220498542e1d01a0cbae3f98c21ea5b4eea6bab94fa9e62b415ad89"


def replace_once(source, old, new):
  if source.count(old) != 1:
    raise ValueError("Unexpected pinned dashboard anchor")
  return source.replace(old, new)


def patch_source(source):
  if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
    raise ValueError("Animation overlap requires the exact pinned PR145 dashboard")
  text = source.decode()
  text = replace_once(text, "render_finish() {\n  local duration effect_canvas_width\n", """FINISH_EFFECT_ATTEMPTED=no
FINALIZATION_FRAME_ATTEMPTED=no

animation_marker() {
  local animation_uptime animation_idle animation_boot_id
  read -r animation_uptime animation_idle </proc/uptime || return 0
  read -r animation_boot_id </proc/sys/kernel/random/boot_id || return 0
  printf 'OMARCHY_BENCHMARK_ANIMATION %s %s %s %s\\n' \\
    "$1" "$animation_boot_id" "$animation_uptime" "$2" >>"$LOG_FILE" 2>/dev/null || true
  if [[ -c /dev/ttyS0 ]]; then
    printf 'OMARCHY_BENCHMARK_ANIMATION %s %s %s %s\\n' \\
      "$1" "$animation_boot_id" "$animation_uptime" "$2" >/dev/ttyS0 || true
  fi
}

render_finish() {
  local duration effect_canvas_width effect_status=0 finish_mode="${1:-complete}"
""")
  text = replace_once(text, '    center "Installed Omarchy in ${duration}" "$CONTENT_WIDTH"\n', """    if [[ $finish_mode == "finalizing" ]]; then
      center "Finalizing Omarchy" "$CONTENT_WIDTH"
    else
      center "Installed Omarchy in ${duration}" "$CONTENT_WIDTH"
    fi
""")
  text = replace_once(text, '    if [[ $TARGET_RELEASED == yes ]]; then\n', """    if [[ $finish_mode == "finalizing" ]]; then
      center "${DIM}Keep the install medium connected${RESET}" "$CONTENT_WIDTH"
    elif [[ $TARGET_RELEASED == yes ]]; then
""")
  text = replace_once(text, '  if [[ -f $LOGO_PATH ]]; then\n    # Reuse mode', '  if [[ -f $LOGO_PATH && $FINISH_EFFECT_ATTEMPTED != "yes" ]]; then\n    # Reuse mode')
  text = replace_once(text, '    timeout 8s ttfx -i "$LOGO_PATH" \\\n', """    FINISH_EFFECT_ATTEMPTED=yes
    animation_marker begin "$finish_mode"
    timeout 8s ttfx -i "$LOGO_PATH" \\
""")
  text = replace_once(text, '      >"$TTY_PATH" 2>/dev/null || dashboard_log "logo effect skipped (ttfx exited $?)"\n', """      >"$TTY_PATH" 2>/dev/null || {
        effect_status=$?
        dashboard_log "logo effect skipped (ttfx exited $effect_status)"
      }
    animation_marker end "$effect_status"
""")
  text = replace_once(text, '  render_dynamic >"$TTY_PATH" 2>/dev/null || true\n  sleep 0.5\n', """  render_dynamic >"$TTY_PATH" 2>/dev/null || true
  if [[ $FINALIZATION_FRAME_ATTEMPTED == "no" && ${OMARCHY_UI_DEFER_PROVISIONING:-} != "yes" && -f $LOGO_PATH ]] &&
     [[ $(jq -r '.current_phase // empty' "$STATE_FILE" 2>/dev/null) == "Finalizing boot and user setup" ]]; then
    FINALIZATION_FRAME_ATTEMPTED=yes
    # The installer is already a child. Keep this complete effect foreground;
    # it must never make a completion or medium-removal promise before wait.
    render_finish finalizing 2>/dev/null || dashboard_log "early finalizing frame failed"
    printf '%s' "$HIDE_CURSOR" >"$TTY_PATH" 2>/dev/null || true
    render_static >"$TTY_PATH" 2>/dev/null || true
    render_dynamic >"$TTY_PATH" 2>/dev/null || true
  fi
  sleep 0.5
""")
  return text.encode()
