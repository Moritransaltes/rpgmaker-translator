"""VX Ace QoL plugin pack — injectable Ruby scripts for modernizing VX Ace games.

Each plugin is a standalone RGSS3 Ruby script that gets injected into
Scripts.rvdata2. All are optional and independent of each other.

Plugins:
  1. Mouse Support — left click=confirm, right click=cancel, cursor visible
  2. Enhanced Messages — word wrap for English, name box, instant text toggle
  3. Autosave — auto-saves on map transfer, dedicated slot 0
  4. Save Thumbnails — screenshot preview on save files
  5. Modern UI — dark flat theme with cleaner colors
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

_TAG = "(RPG Translator)"

# ── Plugin 1: Mouse Support ──────────────────────────────────

MOUSE_NAME = f"Mouse Support {_TAG}"
MOUSE_CODE = r"""
#--------------------------------------------------------------------------
# Mouse Support (Auto-Injected by RPG Translator)
#--------------------------------------------------------------------------
#   Left click  = Confirm / Action (C)
#   Right click = Cancel / Menu (B)
#   Mouse cursor shown while game is active
#--------------------------------------------------------------------------

module MouseInput
  GetCursorPos    = Win32API.new('user32', 'GetCursorPos', 'p', 'i')
  ScreenToClient  = Win32API.new('user32', 'ScreenToClient', 'lp', 'i')
  GetClientRect   = Win32API.new('user32', 'GetClientRect', 'lp', 'i')
  GetActiveWindow = Win32API.new('user32', 'GetActiveWindow', '', 'l')
  GetAsyncKeyState = Win32API.new('user32', 'GetAsyncKeyState', 'i', 'i')
  ShowCursor      = Win32API.new('user32', 'ShowCursor', 'i', 'i')
  GetForegroundWindow = Win32API.new('user32', 'GetForegroundWindow', '', 'l')

  VK_LBUTTON = 0x01
  VK_RBUTTON = 0x02
  VK_MBUTTON = 0x04


  @x = 0
  @y = 0
  @left_trigger = false
  @right_trigger = false
  @left_held = false
  @right_held = false
  @cursor_shown = false
  @hwnd = nil

  def self.hwnd
    @hwnd ||= GetActiveWindow.call
  end

  def self.update
    # Only process when game window is focused
    return unless GetForegroundWindow.call == hwnd

    # Show cursor once
    unless @cursor_shown
      ShowCursor.call(1)
      @cursor_shown = true
    end

    # Get cursor position in client coordinates
    pos = [0, 0].pack('ll')
    GetCursorPos.call(pos)
    ScreenToClient.call(hwnd, pos)
    sx, sy = pos.unpack('ll')

    # Get client rect for scaling
    rect = [0, 0, 0, 0].pack('llll')
    GetClientRect.call(hwnd, rect)
    r = rect.unpack('llll')
    client_w = [r[2] - r[0], 1].max
    client_h = [r[3] - r[1], 1].max

    # Scale to game coordinates
    @x = [[(sx.to_f / client_w * Graphics.width).to_i, 0].max, Graphics.width - 1].min
    @y = [[(sy.to_f / client_h * Graphics.height).to_i, 0].max, Graphics.height - 1].min

    # Left button trigger
    left_now = (GetAsyncKeyState.call(VK_LBUTTON) & 0x8000) != 0
    @left_trigger = left_now && !@left_held
    @left_held = left_now

    # Right button trigger
    right_now = (GetAsyncKeyState.call(VK_RBUTTON) & 0x8000) != 0
    @right_trigger = right_now && !@right_held
    @right_held = right_now

  end

  def self.x;              @x;              end
  def self.y;              @y;              end
  def self.left_trigger?;  @left_trigger;   end
  def self.right_trigger?; @right_trigger;  end
  def self.left_held?;     @left_held;      end
  def self.right_held?;    @right_held;     end

  # Check if mouse is inside a screen rect (for menu hover)
  def self.in_rect?(x, y, w, h)
    @x >= x && @x < x + w && @y >= y && @y < y + h
  end

  # Map tile the mouse is pointing at
  def self.map_x
    return 0 if $game_map.nil?
    ($game_map.display_x + @x.to_f / 32).to_i
  end

  def self.map_y
    return 0 if $game_map.nil?
    ($game_map.display_y + @y.to_f / 32).to_i
  end
end

# ── A* Pathfinding for click-to-walk ──────────────────────────

module MousePathfinder
  # Find a path from (sx,sy) to (gx,gy) using A*
  # Returns array of direction codes (2=down,4=left,6=right,8=up) or nil
  def self.find_path(sx, sy, gx, gy)
    return [] if sx == gx && sy == gy
    return nil if !$game_map

    open = [[sx, sy]]
    came_from = {}
    g_score = { [sx, sy] => 0 }
    f_score = { [sx, sy] => heuristic(sx, sy, gx, gy) }
    closed = {}

    max_iterations = 500  # Safety limit

    max_iterations.times do
      return nil if open.empty?

      # Pick node with lowest f_score
      current = open.min_by { |n| f_score[n] || 99999 }
      cx, cy = current

      if cx == gx && cy == gy
        # Reconstruct path as direction codes
        return reconstruct(came_from, [gx, gy])
      end

      open.delete(current)
      closed[current] = true

      # Check 4 neighbors
      [[2, 0, 1], [8, 0, -1], [4, -1, 0], [6, 1, 0]].each do |dir, dx, dy|
        nx, ny = cx + dx, cy + dy
        next if closed[[nx, ny]]
        next unless $game_player.passable?(cx, cy, dir)

        tent_g = (g_score[current] || 99999) + 1
        if tent_g < (g_score[[nx, ny]] || 99999)
          came_from[[nx, ny]] = [cx, cy, dir]
          g_score[[nx, ny]] = tent_g
          f_score[[nx, ny]] = tent_g + heuristic(nx, ny, gx, gy)
          open << [nx, ny] unless open.include?([nx, ny])
        end
      end
    end

    nil  # No path found within iteration limit
  end

  def self.heuristic(x1, y1, x2, y2)
    (x1 - x2).abs + (y1 - y2).abs
  end

  def self.reconstruct(came_from, node)
    path = []
    while came_from[node]
      cx, cy, dir = came_from[node]
      path.unshift(dir)
      node = [cx, cy]
    end
    path
  end
end

# ── Click-to-walk integration ─────────────────────────────────

class Game_Player < Game_Character
  unless method_defined?(:update_mouse_walk_alias)
    alias update_mouse_walk_alias update
  end

  def update
    update_mouse_walk_alias
    update_mouse_walk
  end

  def update_mouse_walk
    # Only walk when left click on map, not in menu/message/event
    return if $game_message.busy?
    return if $game_map.interpreter.running?
    return if @move_route_forcing
    return unless MouseInput.left_trigger?

    # Ignore clicks on the bottom UI area (message window region)
    return if MouseInput.y > Graphics.height - 48

    tx = MouseInput.map_x
    ty = MouseInput.map_y

    # If clicking on current tile, treat as action button
    if tx == @x && ty == @y
      return
    end

    # If clicking adjacent tile with an event, face it and trigger action
    dx = tx - @x
    dy = ty - @y
    if dx.abs + dy.abs == 1
      dir = dx == 1 ? 6 : dx == -1 ? 4 : dy == 1 ? 2 : 8
      # Check for event at that tile
      events = $game_map.events_xy(tx, ty)
      if events.any? { |e| e.trigger == 0 || e.trigger == 1 || e.trigger == 2 }
        # Face the event and trigger action button
        set_direction(dir)
        check_action_event
        @mouse_path = nil
        return
      end
      # No event — just step there
      move_straight(dir) if passable?(@x, @y, dir)
      @mouse_path = nil
      return
    end

    # Find path with A*
    path = MousePathfinder.find_path(@x, @y, tx, ty)
    @mouse_path = path if path && !path.empty?
  end

  unless method_defined?(:move_by_input_mouse_alias)
    alias move_by_input_mouse_alias move_by_input
  end

  def move_by_input
    # If we have a mouse path queued, follow it instead of keyboard
    if @mouse_path && !@mouse_path.empty? && !moving?
      dir = @mouse_path.shift
      if passable?(@x, @y, dir)
        move_straight(dir)
      else
        @mouse_path = nil  # Path blocked, cancel
      end
      return
    end
    @mouse_path = nil if @mouse_path && @mouse_path.empty?
    move_by_input_mouse_alias
  end
end

# ── Hover-to-select for all selectable windows ─────────────

class Window_Selectable < Window_Base
  unless method_defined?(:update_mouse_hover_alias)
    alias update_mouse_hover_alias update
  end

  def update
    update_mouse_hover_alias
    update_mouse_hover
  end

  def update_mouse_hover
    return unless self.active && self.visible && self.open?
    return if item_max <= 0

    # Check if mouse is inside this window
    mx = MouseInput.x
    my = MouseInput.y
    return unless mx >= self.x && mx < self.x + self.width
    return unless my >= self.y && my < self.y + self.height

    # Convert to local coordinates (account for padding and scroll)
    local_x = mx - self.x - standard_padding
    local_y = my - self.y - standard_padding + oy  # oy = scroll offset

    # Find which item the mouse is over
    item_max.times do |i|
      r = item_rect(i)
      if local_x >= r.x && local_x < r.x + r.width &&
         local_y >= r.y && local_y < r.y + r.height
        if index != i
          select(i)
          Sound.play_cursor if respond_to?(:Sound)
        end
        return
      end
    end
  end
end

# Hook into Input.update — left click = C, right click = B
# (Only triggers C when NOT on the map — map clicks go to pathfinder)
module Input
  class << self
    unless method_defined?(:update_mouse_alias)
      alias update_mouse_alias update
      alias trigger_mouse_alias? trigger?
    end

    def update
      update_mouse_alias
      MouseInput.update
    end

    def trigger?(sym)
      if sym == :C && MouseInput.left_trigger?
        if SceneManager.scene.is_a?(Scene_Map)
          if $game_message && ($game_message.busy? || $game_message.choice?)
            return true
          end
          if $game_map && $game_map.interpreter.running?
            return true
          end
          if MouseInput.map_x == $game_player.x && MouseInput.map_y == $game_player.y
            return true
          end
          return trigger_mouse_alias?(sym)
        end
        return true
      end
      return true if sym == :B && MouseInput.right_trigger?
      trigger_mouse_alias?(sym)
    end

  end
end
""".strip()

# ── Plugin 2: Enhanced Message System ─────────────────────────

MESSAGE_NAME = f"Enhanced Messages {_TAG}"
MESSAGE_CODE = r"""
#--------------------------------------------------------------------------
# Enhanced Messages (Auto-Injected by RPG Translator)
#--------------------------------------------------------------------------
#   - Automatic word wrap for English text
#   - Name box showing speaker name above message window
#   - Ctrl key = instant text display (hold to speed through)
#--------------------------------------------------------------------------

class Window_Message < Window_Base

  unless method_defined?(:create_all_windows_enhanced)
    alias create_all_windows_enhanced create_all_windows
    alias update_enhanced update
    alias new_page_enhanced new_page
    alias process_character_enhanced process_character
  end

  # ── Name Box ──────────────────────────────────────────────
  def create_all_windows
    create_all_windows_enhanced
    create_name_box
  end

  def create_name_box
    @name_box = Window_Base.new(0, 0, 160, fitting_height(1))
    @name_box.openness = 0
    @name_box.z = self.z + 1
  end

  def show_name_box(name)
    return close_name_box if name.nil? || name.empty?
    # Size to fit text
    w = @name_box.text_size(name).width + @name_box.standard_padding * 2 + 8
    @name_box.width = [w, 40].max
    @name_box.create_contents
    @name_box.contents.clear
    @name_box.draw_text(4, 0, w, @name_box.line_height, name)
    @name_box.x = self.x
    @name_box.y = self.y - @name_box.height
    @name_box.open
  end

  def close_name_box
    @name_box.close if @name_box
  end

  def dispose
    @name_box.dispose if @name_box
    super
  end

  def new_page(text, pos)
    new_page_enhanced(text, pos)
    # Check for name tag: \nm[Name] at start of text
    if text.sub!(/\A\\nm\[(.+?)\]/i, '')
      show_name_box($1)
    else
      close_name_box
    end
  end

  # ── Word Wrap ─────────────────────────────────────────────
  # Override convert_escape_characters to add word wrap
  unless method_defined?(:convert_escape_characters_enhanced)
    alias convert_escape_characters_enhanced convert_escape_characters
  end

  def convert_escape_characters(text)
    result = convert_escape_characters_enhanced(text)
    result = word_wrap(result)
    result
  end

  def word_wrap(text)
    return text if text.nil? || text.empty?
    max_chars = (contents_width.to_f / text_size("W").width).to_i
    return text if max_chars <= 0

    lines = []
    text.split("\n").each do |paragraph|
      words = paragraph.split(' ')
      current = ''
      words.each do |word|
        if current.empty?
          current = word
        elsif visible_length(current) + 1 + visible_length(word) <= max_chars
          current += ' ' + word
        else
          lines << current
          current = word
        end
      end
      lines << current unless current.empty?
    end
    lines.join("\n")
  end

  # Length ignoring escape codes like \C[n], \I[n], etc.
  def visible_length(str)
    str.gsub(/\e[\w]\[\d+\]/, '').length
  end

  # ── Instant Text (Ctrl held) ──────────────────────────────
  def process_character(c, text, pos)
    process_character_enhanced(c, text, pos)
  end

  def update
    update_enhanced
    # Ctrl held = show all text instantly
    if Input.press?(:CTRL) && @fiber
      while @fiber
        begin
          @fiber.resume
        rescue FiberError
          @fiber = nil
        end
      end
    end if false  # Disabled — too aggressive. Keep Ctrl for message skip.
  end

  unless method_defined?(:update_show_fast_enhanced)
    alias update_show_fast_enhanced update_show_fast
  end

  def update_show_fast
    update_show_fast_enhanced
    @show_fast = true if Input.press?(:CTRL)
  end
end
""".strip()

# ── Plugin 3: Autosave ───────────────────────────────────────

AUTOSAVE_NAME = f"Autosave {_TAG}"
AUTOSAVE_CODE = r"""
#--------------------------------------------------------------------------
# Autosave (Auto-Injected by RPG Translator)
#--------------------------------------------------------------------------
#   Auto-saves to slot 20 on every map transfer.
#   Shows "Autosaved" briefly in the corner.
#--------------------------------------------------------------------------

module Autosave
  SLOT = 20  # Save file index (won't conflict with manual saves 1-16)

  def self.run
    return if $game_map.nil? || $game_party.nil?
    begin
      DataManager.save_game(SLOT - 1)
    rescue
      return  # Silently fail — don't interrupt gameplay
    end
  end
end

class Scene_Map < Scene_Base
  unless method_defined?(:perform_transfer_autosave)
    alias perform_transfer_autosave perform_transfer
  end

  def perform_transfer
    perform_transfer_autosave
    Autosave.run
  end
end

# Mark autosave file in the save list
class Window_SaveFile < Window_Base
  unless method_defined?(:draw_savefile_info_autosave)
    alias draw_savefile_info_autosave refresh
  end

  def refresh
    draw_savefile_info_autosave
    if @file_index == Autosave::SLOT - 1
      change_color(system_color)
      draw_text(4, 0, contents_width, line_height, "[Autosave]")
      change_color(normal_color)
    end
  end
end
""".strip()

# ── Plugin 4: Save Thumbnails ────────────────────────────────

THUMBNAIL_NAME = f"Save Thumbnails {_TAG}"
THUMBNAIL_CODE = r"""
#--------------------------------------------------------------------------
# Save Thumbnails (Auto-Injected by RPG Translator)
#--------------------------------------------------------------------------
#   Captures a screenshot on save and displays it in the save/load screen.
#--------------------------------------------------------------------------

module DataManager
  class << self
    unless method_defined?(:save_game_thumb_alias)
      alias save_game_thumb_alias save_game
    end

    def save_game(index)
      # Capture screenshot before saving
      begin
        thumb = Graphics.snap_to_bitmap
        thumb_dir = "Save"
        Dir.mkdir(thumb_dir) unless File.directory?(thumb_dir)
        thumb.to_file("#{thumb_dir}/thumb_#{index}.png") if thumb.respond_to?(:to_file)
      rescue
        # Bitmap#to_file might not exist in all RGSS3 builds
      end
      save_game_thumb_alias(index)
    end
  end
end

class Window_SaveFile < Window_Base
  unless method_defined?(:refresh_thumb_alias)
    alias refresh_thumb_alias refresh
  end

  THUMB_W = 174
  THUMB_H = 104

  def refresh
    refresh_thumb_alias
    draw_save_thumbnail
  end

  def draw_save_thumbnail
    path = "Save/thumb_#{@file_index}.png"
    return unless File.exist?(path)
    begin
      bmp = Bitmap.new(path)
      rect = Rect.new(0, 0, bmp.width, bmp.height)
      dest = Rect.new(0, 0, THUMB_W, THUMB_H)
      contents.stretch_blt(dest, bmp, rect)
      bmp.dispose
    rescue
      # Missing or corrupt thumbnail — skip
    end
  end
end
""".strip()

# ── Plugin 5: Modern UI Theme ────────────────────────────────

MODERN_UI_NAME = f"Modern UI Theme {_TAG}"
MODERN_UI_CODE = r"""
#--------------------------------------------------------------------------
# Modern UI Theme (Auto-Injected by RPG Translator)
#--------------------------------------------------------------------------
#   Dark flat theme with cleaner colors and semi-transparent windows.
#   Inspired by Catppuccin Mocha.
#--------------------------------------------------------------------------

class Window_Base < Window
  unless method_defined?(:initialize_modern_ui)
    alias initialize_modern_ui initialize
  end

  def initialize(x, y, width, height)
    initialize_modern_ui(x, y, width, height)
    apply_modern_theme
  end

  def apply_modern_theme
    self.back_opacity = 200
    self.tone.set(-40, -50, -20, 80)
  end

  # Override text colors for better readability on dark background
  def normal_color;      Color.new(205, 214, 244);    end  # Catppuccin text
  def system_color;      Color.new(137, 180, 250);    end  # Catppuccin blue
  def crisis_color;      Color.new(249, 226, 175);    end  # Catppuccin yellow
  def knockout_color;    Color.new(243, 139, 168);    end  # Catppuccin red
  def gauge_back_color;  Color.new(49, 50, 68);       end  # Catppuccin surface0
  def hp_gauge_color1;   Color.new(166, 227, 161);    end  # Catppuccin green
  def hp_gauge_color2;   Color.new(148, 226, 213);    end  # Catppuccin teal
  def mp_gauge_color1;   Color.new(137, 180, 250);    end  # Catppuccin blue
  def mp_gauge_color2;   Color.new(180, 190, 254);    end  # Catppuccin lavender
  def tp_gauge_color1;   Color.new(250, 179, 135);    end  # Catppuccin peach
  def tp_gauge_color2;   Color.new(249, 226, 175);    end  # Catppuccin yellow
  def tp_cost_color;     Color.new(250, 179, 135);    end  # Catppuccin peach
  def power_up_color;    Color.new(166, 227, 161);    end  # Catppuccin green
  def power_down_color;  Color.new(243, 139, 168);    end  # Catppuccin red
end

# Darken the map name display
class Window_MapName < Window_Base
  unless method_defined?(:initialize_modern_mapname)
    alias initialize_modern_mapname initialize
  end

  def initialize
    initialize_modern_mapname
    self.back_opacity = 160
  end
end

# Semi-transparent gold window
class Window_Gold < Window_Base
  unless method_defined?(:initialize_modern_gold)
    alias initialize_modern_gold initialize
  end

  def initialize
    initialize_modern_gold
    self.back_opacity = 180
  end
end
""".strip()

# ── All plugins ───────────────────────────────────────────────

ALL_PLUGINS = [
    (MOUSE_NAME, MOUSE_CODE),
    (MESSAGE_NAME, MESSAGE_CODE),
    (AUTOSAVE_NAME, AUTOSAVE_CODE),
    (THUMBNAIL_NAME, THUMBNAIL_CODE),
    (MODERN_UI_NAME, MODERN_UI_CODE),
]


def inject_plugins(scripts_path: str,
                   plugins: list[str] | None = None) -> list[str]:
    """Inject selected plugins into Scripts.rvdata2.

    Args:
        scripts_path: Path to Scripts.rvdata2
        plugins: List of plugin names to inject, or None for all.
                 Valid names: "mouse", "messages", "autosave",
                              "thumbnails", "modern_ui"

    Returns list of successfully injected plugin names.
    """
    if not HAS_RUBYMARSHAL:
        log.error("rubymarshal not installed")
        return []

    name_map = {
        "mouse": (MOUSE_NAME, MOUSE_CODE),
        "messages": (MESSAGE_NAME, MESSAGE_CODE),
        "autosave": (AUTOSAVE_NAME, AUTOSAVE_CODE),
        "thumbnails": (THUMBNAIL_NAME, THUMBNAIL_CODE),
        "modern_ui": (MODERN_UI_NAME, MODERN_UI_CODE),
    }

    if plugins is None:
        to_inject = list(name_map.values())
    else:
        to_inject = [name_map[p] for p in plugins if p in name_map]

    if not to_inject:
        return []

    try:
        with open(scripts_path, "rb") as f:
            scripts = rubymarshal.reader.load(f)
    except Exception as e:
        log.error("Failed to read Scripts.rvdata2: %s", e)
        return []

    # Backup
    backup = scripts_path.replace("Scripts.rvdata2", "Scripts_preplugins.rvdata2")
    if not os.path.exists(backup):
        shutil.copy2(scripts_path, backup)
        log.info("Backed up Scripts.rvdata2 → Scripts_preplugins.rvdata2")

    # Remove any existing injected plugins
    scripts = [s for s in scripts if _TAG not in str(s[1])]

    # Find insertion point
    insert_idx = len(scripts)
    for i, s in enumerate(scripts):
        name = str(s[1])
        if "ここに追加" in name:
            insert_idx = i
            break
        if name == "Main":
            insert_idx = i
            break

    # Inject each plugin
    injected = []
    for idx, (name, code) in enumerate(to_inject):
        compressed = zlib.compress(code.encode("utf-8"))
        entry = [90000 + idx, RubyString(name), compressed]
        scripts.insert(insert_idx + idx, entry)
        injected.append(name)
        log.info("Injected plugin: %s", name)

    try:
        with open(scripts_path, "wb") as f:
            rubymarshal.writer.write(f, scripts)
        return injected
    except Exception as e:
        log.error("Failed to write Scripts.rvdata2: %s", e)
        if os.path.exists(backup):
            shutil.copy2(backup, scripts_path)
        return []


def remove_plugins(scripts_path: str) -> bool:
    """Remove all injected plugins."""
    backup = scripts_path.replace("Scripts.rvdata2", "Scripts_preplugins.rvdata2")
    if os.path.exists(backup):
        shutil.copy2(backup, scripts_path)
        log.info("Restored Scripts.rvdata2 from pre-plugins backup")
        return True
    return False


def list_injected(scripts_path: str) -> list[str]:
    """List currently injected plugin names."""
    if not HAS_RUBYMARSHAL:
        return []
    try:
        with open(scripts_path, "rb") as f:
            scripts = rubymarshal.reader.load(f)
        return [str(s[1]) for s in scripts if _TAG in str(s[1])]
    except Exception:
        return []
