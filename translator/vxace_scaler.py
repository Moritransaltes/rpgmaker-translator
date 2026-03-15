"""VX Ace window scaler — inject a Win32 API resize script into Scripts.rvdata2.

Reads the game's current resolution from the existing scripts, offers scale
options, and injects a small Ruby script that:
  - Sets a default window scale on launch
  - PgUp = scale up, PgDn = scale down (1x / 1.5x / 2x / 2.5x / 3x)
"""

import logging
import os
import shutil
import zlib

log = logging.getLogger(__name__)

try:
    import rubymarshal.reader
    import rubymarshal.writer
    from rubymarshal.classes import RubyString
    HAS_RUBYMARSHAL = True
except ImportError:
    HAS_RUBYMARSHAL = False

SCRIPT_NAME = "Window Scaler (Auto-Injected)"

_SCRIPT_CODE = r"""
#--------------------------------------------------------------------------
# Window Scaler (Auto-Injected by RPG Translator)
#--------------------------------------------------------------------------
#   PgUp = Scale window up    (1x > 1.5x > 2x > 2.5x > 3x)
#   PgDn = Scale window down  (3x > 2.5x > 2x > 1.5x > 1x)
#   F3   = Cycle: Off > Borderless (ratio kept) > Stretched > Off
#   Default: {default_scale}x on launch
#--------------------------------------------------------------------------

module WindowScaler
  GetActiveWindow   = Win32API.new('user32', 'GetActiveWindow', '', 'l')
  MoveWindow        = Win32API.new('user32', 'MoveWindow', 'liiiil', 'l')
  GetSystemMetrics  = Win32API.new('user32', 'GetSystemMetrics', 'i', 'i')
  GetWindowLong     = Win32API.new('user32', 'GetWindowLongA', 'li', 'l')
  SetWindowLong     = Win32API.new('user32', 'SetWindowLongA', 'lil', 'l')
  AdjustWindowRect  = Win32API.new('user32', 'AdjustWindowRect', 'pli', 'l')
  GetAsyncKeyState  = Win32API.new('user32', 'GetAsyncKeyState', 'i', 'i')

  GWL_STYLE     = -16
  SM_CXSCREEN   = 0
  SM_CYSCREEN   = 1
  VK_PRIOR      = 0x21  # PgUp
  VK_NEXT       = 0x22  # PgDn
  VK_F3         = 0x72  # F3
  WS_POPUP      = 0x80000000
  WS_CAPTION    = 0x00C00000
  WS_THICKFRAME = 0x00040000

  SCALES = [1.0, 1.5, 2.0, 2.5, 3.0]
  DEFAULT_INDEX = {default_index}

  # Fullscreen states: 0=off, 1=ratio-kept borderless, 2=stretched borderless
  @scale_index = DEFAULT_INDEX
  @pgup_held = false
  @pgdn_held = false
  @f3_held = false
  @fs_state = 0
  @saved_style = 0

  def self.hwnd
    @hwnd ||= GetActiveWindow.call
  end

  def self.apply_scale
    scale = SCALES[@scale_index]
    target_w = (Graphics.width  * scale).to_i
    target_h = (Graphics.height * scale).to_i

    style = GetWindowLong.call(hwnd, GWL_STYLE)
    rect = [0, 0, target_w, target_h].pack('l4')
    AdjustWindowRect.call(rect, style, 0)
    r = rect.unpack('l4')
    win_w = r[2] - r[0]
    win_h = r[3] - r[1]

    screen_w = GetSystemMetrics.call(SM_CXSCREEN)
    screen_h = GetSystemMetrics.call(SM_CYSCREEN)
    x = [(screen_w - win_w) / 2, 0].max
    y = [(screen_h - win_h) / 2, 0].max

    MoveWindow.call(hwnd, x, y, win_w, win_h, 1)
  end

  def self.scale_up
    return if @fs_state != 0
    if @scale_index < SCALES.length - 1
      @scale_index += 1
      apply_scale
    end
  end

  def self.scale_down
    return if @fs_state != 0
    if @scale_index > 0
      @scale_index -= 1
      apply_scale
    end
  end

  def self.cycle_fullscreen
    case @fs_state
    when 0
      # Off -> Ratio-kept borderless
      enter_borderless_ratio
    when 1
      # Ratio-kept -> Stretched
      enter_borderless_stretched
    when 2
      # Stretched -> Off (back to windowed)
      exit_borderless
    end
  end

  def self.enter_borderless_ratio
    @saved_style = GetWindowLong.call(hwnd, GWL_STYLE)
    new_style = @saved_style & ~WS_CAPTION & ~WS_THICKFRAME | WS_POPUP
    SetWindowLong.call(hwnd, GWL_STYLE, new_style)

    screen_w = GetSystemMetrics.call(SM_CXSCREEN)
    screen_h = GetSystemMetrics.call(SM_CYSCREEN)
    game_ratio = Graphics.width.to_f / Graphics.height

    fit_h = screen_h
    fit_w = (fit_h * game_ratio).to_i
    if fit_w > screen_w
      fit_w = screen_w
      fit_h = (fit_w / game_ratio).to_i
    end

    x = (screen_w - fit_w) / 2
    y = (screen_h - fit_h) / 2
    MoveWindow.call(hwnd, x, y, fit_w, fit_h, 1)
    @fs_state = 1
  end

  def self.enter_borderless_stretched
    # Already borderless from state 1, just resize to full screen
    screen_w = GetSystemMetrics.call(SM_CXSCREEN)
    screen_h = GetSystemMetrics.call(SM_CYSCREEN)
    MoveWindow.call(hwnd, 0, 0, screen_w, screen_h, 1)
    @fs_state = 2
  end

  def self.exit_borderless
    SetWindowLong.call(hwnd, GWL_STYLE, @saved_style)
    @fs_state = 0
    apply_scale
  end

  def self.check_keys
    # PgUp
    pgup_now = (GetAsyncKeyState.call(VK_PRIOR) & 0x8000) != 0
    if pgup_now && !@pgup_held
      scale_up
    end
    @pgup_held = pgup_now

    # PgDn
    pgdn_now = (GetAsyncKeyState.call(VK_NEXT) & 0x8000) != 0
    if pgdn_now && !@pgdn_held
      scale_down
    end
    @pgdn_held = pgdn_now

    # F3 — cycle: off > ratio-kept > stretched > off
    f3_now = (GetAsyncKeyState.call(VK_F3) & 0x8000) != 0
    if f3_now && !@f3_held
      cycle_fullscreen
    end
    @f3_held = f3_now
  end
end

# Apply default scale on startup
WindowScaler.apply_scale

# Hook into the main update loop for key detection
module Graphics
  class << self
    unless method_defined?(:update_scaler_alias)
      alias update_scaler_alias update
    end

    def update
      update_scaler_alias
      WindowScaler.check_keys
    end
  end
end
""".strip()


def detect_resolution(scripts_path: str) -> tuple[int, int]:
    """Detect the game's internal resolution from Scripts.rvdata2.

    Looks for Graphics.resize_screen(w, h) calls in scripts.
    Falls back to the VX Ace default (544x416).
    """
    if not HAS_RUBYMARSHAL:
        return 544, 416

    try:
        with open(scripts_path, "rb") as f:
            scripts = rubymarshal.reader.load(f)
    except Exception:
        return 544, 416

    import re
    for s in scripts:
        try:
            code = zlib.decompress(bytes(s[2])).decode("utf-8", errors="replace")
        except Exception:
            continue
        m = re.search(r'Graphics\.resize_screen\(\s*(\d+)\s*,\s*(\d+)\s*\)', code)
        if m:
            return int(m.group(1)), int(m.group(2))

    return 544, 416


def is_already_injected(scripts_path: str) -> bool:
    """Check if the scaler script is already present."""
    if not HAS_RUBYMARSHAL:
        return False
    try:
        with open(scripts_path, "rb") as f:
            scripts = rubymarshal.reader.load(f)
        for s in scripts:
            if SCRIPT_NAME in str(s[1]):
                return True
    except Exception:
        pass
    return False


def inject_scaler(scripts_path: str, default_scale: float = 2.0) -> bool:
    """Inject the window scaler script into Scripts.rvdata2.

    Creates a backup (Scripts_prescaler.rvdata2) before modifying.
    If a scaler was previously injected, it is replaced.

    Args:
        scripts_path: Path to Scripts.rvdata2
        default_scale: Initial scale factor (1.0, 1.5, 2.0, 2.5, or 3.0)

    Returns True on success.
    """
    if not HAS_RUBYMARSHAL:
        log.error("rubymarshal not installed — cannot inject scaler")
        return False

    scales = [1.0, 1.5, 2.0, 2.5, 3.0]
    default_index = scales.index(default_scale) if default_scale in scales else 2

    try:
        with open(scripts_path, "rb") as f:
            scripts = rubymarshal.reader.load(f)
    except Exception as e:
        log.error("Failed to read Scripts.rvdata2: %s", e)
        return False

    # Backup (only first time)
    backup = scripts_path.replace("Scripts.rvdata2", "Scripts_prescaler.rvdata2")
    if not os.path.exists(backup):
        shutil.copy2(scripts_path, backup)
        log.info("Backed up Scripts.rvdata2 → Scripts_prescaler.rvdata2")

    # Remove any existing scaler script
    scripts = [s for s in scripts if SCRIPT_NAME not in str(s[1])]

    # Build the script code
    code = _SCRIPT_CODE.format(
        default_scale=default_scale,
        default_index=default_index,
    )
    compressed = zlib.compress(code.encode("utf-8"))

    # Find insertion point: before "ここに追加" or before Main
    insert_idx = len(scripts)
    for i, s in enumerate(scripts):
        name = str(s[1])
        if "ここに追加" in name:
            insert_idx = i
            break
        if name == "Main":
            insert_idx = i
            break

    new_entry = [99999, RubyString(SCRIPT_NAME), compressed]
    scripts.insert(insert_idx, new_entry)

    try:
        with open(scripts_path, "wb") as f:
            rubymarshal.writer.write(f, scripts)
        base_w, base_h = detect_resolution(scripts_path)
        log.info("Injected window scaler: %dx%d default %sx, F5=cycle, F6=fullscreen",
                 base_w, base_h, default_scale)
        return True
    except Exception as e:
        log.error("Failed to write Scripts.rvdata2: %s", e)
        # Restore backup
        if os.path.exists(backup):
            shutil.copy2(backup, scripts_path)
        return False


def remove_scaler(scripts_path: str) -> bool:
    """Remove the injected scaler script and restore original."""
    backup = scripts_path.replace("Scripts.rvdata2", "Scripts_prescaler.rvdata2")
    if os.path.exists(backup):
        shutil.copy2(backup, scripts_path)
        log.info("Restored Scripts.rvdata2 from pre-scaler backup")
        return True

    # No backup — try to remove the script entry manually
    if not HAS_RUBYMARSHAL:
        return False
    try:
        with open(scripts_path, "rb") as f:
            scripts = rubymarshal.reader.load(f)
        scripts = [s for s in scripts if SCRIPT_NAME not in str(s[1])]
        with open(scripts_path, "wb") as f:
            rubymarshal.writer.write(f, scripts)
        return True
    except Exception:
        return False
