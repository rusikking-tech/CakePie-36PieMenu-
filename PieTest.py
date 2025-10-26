import sys
import os
import json
import math
import threading 
import time
from pathlib import Path
from typing import List, Dict, Optional, Union

from PyQt5 import QtCore, QtGui, QtWidgets
import keyboard
import mouse 

# ------------------------------
# Конфигурация / utils
# ------------------------------

# ИСПРАВЛЕНИЕ ДЛЯ EXE: Определение пути к папке скрипта/исполняемого файла
if getattr(sys, 'frozen', False):
    # Приложение упаковано (EXE)
    SCRIPT_DIR = Path(sys.executable).parent
else:
    # Обычный скрипт Python
    SCRIPT_DIR = Path(__file__).parent
    
CONFIG_PATH = SCRIPT_DIR / "radial_config.json"

DEFAULT_SUBMENU_CONFIG = {
    "submenu_radius": 110,     # Расстояние элементов подменю от центра (px)
    "threshold_ratio": 0.6,    # Коэффициент порога от main_radius (0.1 - 1.0)
    "item_size": 30            # Радиус элементов подменю (шариков) (px)
}

DEFAULT_CONFIG = {
    "activation": {
        "combo": "alt+x" 
    },
    "visual": {
        "main_radius": 60,         # Радиус главного меню/порога (px)
        "timer_interval_ms": 25,   # Интервал таймера мониторинга (ms)
        "theme": "black_red"
    },
    "directions": {
        "north": {"label": "North", "items": [], **DEFAULT_SUBMENU_CONFIG},
        "east": {"label": "East", "items": [], **DEFAULT_SUBMENU_CONFIG},
        "south": {"label": "South", "items": [], **DEFAULT_SUBMENU_CONFIG},
        "west": {"label": "West", "items": [], **DEFAULT_SUBMENU_CONFIG}
    }
}

def load_config() -> Dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            
            # Миграция и дефолты для активации и визуальных настроек
            if "activation" in cfg and "combo" not in cfg["activation"]:
                mod = cfg["activation"].pop("modifier", "alt")
                key = cfg["activation"].pop("key", "x")
                cfg["activation"]["combo"] = f"{mod}+{key}"
                
            vis_cfg = cfg.get("visual", {})
            main_rad = vis_cfg.pop("radius", None) 
            if main_rad is None: 
                main_rad = vis_cfg.get("main_radius", DEFAULT_CONFIG["visual"]["main_radius"])
            vis_cfg["main_radius"] = main_rad

            # Удаление старых глобальных параметров, если они есть
            vis_cfg.pop("threshold", None) 
            vis_cfg.pop("submenu_radius", None) 
            vis_cfg.pop("threshold_ratio", None) 
            
            if "timer_interval_ms" not in vis_cfg: vis_cfg["timer_interval_ms"] = DEFAULT_CONFIG["visual"]["timer_interval_ms"]
            
            cfg["visual"] = vis_cfg
            
            # --- Per-Submenu Migration/Defaulting ---
            for d in ["north", "east", "south", "west"]:
                dir_cfg = cfg.get("directions", {}).get(d, {})
                
                # Применение дефолтов, если отсутствуют
                if "submenu_radius" not in dir_cfg:
                    dir_cfg["submenu_radius"] = DEFAULT_SUBMENU_CONFIG["submenu_radius"] 
                
                if "threshold_ratio" not in dir_cfg:
                    dir_cfg["threshold_ratio"] = DEFAULT_SUBMENU_CONFIG["threshold_ratio"] 
                
                if "item_size" not in dir_cfg:
                    dir_cfg["item_size"] = DEFAULT_SUBMENU_CONFIG["item_size"]
                    
                cfg["directions"][d] = dir_cfg


            return cfg
            
    except Exception:
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()

def save_config(cfg: Dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print("Failed saving config:", e)

CONFIG = load_config()

# ------------------------------
# Overlay (визуальное меню)
# ------------------------------

# Установим фиксированный размер окна (достаточно большой для всех меню)
OVERLAY_WINDOW_SIZE = 500 
OVERLAY_LOCAL_CENTER = OVERLAY_WINDOW_SIZE // 2 # 250

class RadialOverlay(QtWidgets.QWidget):
    
    # Сигнал для перехода на подменю 
    direction_passed_threshold = QtCore.pyqtSignal(str) 
    _last_selected_direction: Optional[str] = None
    
    SUBMENU_COLORS = {
        'north': QtGui.QColor(200, 20, 20, 255),  # Красный
        'east': QtGui.QColor(255, 200, 0, 255),   # Жёлтый
        'south': QtGui.QColor(20, 180, 20, 255),  # Зелёный
        'west': QtGui.QColor(20, 20, 200, 255)    # Синий
    }

    def __init__(self, cfg: Dict):
        super().__init__(None, QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.FramelessWindowHint | QtCore.Qt.Tool)
        self.cfg = cfg
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        
        # --- ИЗМЕНЕНИЕ 1: Фиксируем размер окна вместо полноэкранного режима ---
        self.setFixedSize(OVERLAY_WINDOW_SIZE, OVERLAY_WINDOW_SIZE)
        # --- КОНЕЦ ИЗМЕНЕНИЯ 1 ---
        
        self.setMouseTracking(True) 
        
        self.active = False
        # КООРДИНАТЫ ЦЕНТРА МЕНЮ (теперь это центр окна OVERLAY_WINDOW_SIZE)
        self.center_x = OVERLAY_LOCAL_CENTER
        self.center_y = OVERLAY_LOCAL_CENTER
        
        self.menu_level = 0 
        self.menu_data: Union[Dict, List] = {} 
        self.current_direction = None 
        self.highlight_index: Optional[int] = None 
        
        vis_cfg = cfg.get("visual", DEFAULT_CONFIG["visual"])
        
        self.main_radius = vis_cfg.get("main_radius", DEFAULT_CONFIG["visual"]["main_radius"])
        
        self.current_submenu_radius = 0 
        self.current_threshold = 0 
        self.current_item_size = 0 
        
        self.preview_direction = None

        self._tooltip_timer = QtCore.QTimer(self)
        self._tooltip_timer.setSingleShot(True)
        self._tooltip_timer.timeout.connect(self._hide_tooltip)
        
        self._monitor_timer = QtCore.QTimer(self)
        self._monitor_timer.setInterval(16)  
        self._monitor_timer.timeout.connect(self.update)

    def _show_tooltip(self, text: str):
        """Отображает всплывающую подсказку с полным текстом."""
        QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), text, self, self.rect())
        self._tooltip_timer.start(5000) 

    def _hide_tooltip(self):
        """Скрывает всплывающую подсказку."""
        QtWidgets.QToolTip.hideText()
        
    def open_main_menu(self, x: int, y: int):
        
        # --- ИЗМЕНЕНИЕ 2: Позиционирование окна ДО show(), чтобы избежать прыжка ---
        # x, y - глобальные координаты курсора
        # Вычисляем верхний левый угол (UL), чтобы центр 500x500 окна был на (x, y)
        ul_x = x - OVERLAY_LOCAL_CENTER
        ul_y = y - OVERLAY_LOCAL_CENTER
        
        # Устанавливаем позицию окна ДО вызова show()
        self.move(ul_x, ul_y) 
        
        # FИКСИРУЕМ ЦЕНТР МЕНЮ (теперь это локальный центр 250, 250)
        self.center_x = OVERLAY_LOCAL_CENTER
        self.center_y = OVERLAY_LOCAL_CENTER
        # --- КОНЕЦ ИЗМЕНЕНИЯ 2 ---
        
        self.menu_level = 0
        self.menu_data = self.cfg['directions'] 
        self.active = True
        self.current_direction = None
        self.highlight_index = None
        self._last_selected_direction = None 
        self.preview_direction = None
        
        # Включаем игнорирование событий мыши
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._hide_tooltip()

        threshold_ratio = self.menu_data.get('north', {}).get("threshold_ratio", DEFAULT_SUBMENU_CONFIG["threshold_ratio"])
        self.current_threshold = int(self.main_radius * threshold_ratio) 
        
        self.show()
        self._monitor_timer.start()

    def open_submenu(self, direction: str, items: List[Dict]):
        # self.center_x и self.center_y уже зафиксированы в локальном центре окна
        self.menu_level = 1
        self.menu_data = [it for it in items if it.get('keys') or it.get('value')]
        self.current_direction = direction
        
        # При открытии подменю, если элементов нет, highlight_index остаётся None
        # Если элементы есть, выделяем первый (для навигации колесом)
        if len(self.menu_data) > 0:
            self.highlight_index = 0
        else:
            self.highlight_index = None

        self.active = True
        self.preview_direction = None
        
        # Выключаем игнорирование событий мыши, чтобы можно было ловить mouseMoveEvent И wheelEvent
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, False)
        
        dir_cfg = self.cfg['directions'].get(direction, {})
        
        submenu_rad_config = dir_cfg.get("submenu_radius", DEFAULT_SUBMENU_CONFIG["submenu_radius"])
        item_size = dir_cfg.get("item_size", DEFAULT_SUBMENU_CONFIG["item_size"])
        
        # Обеспечение, что "шарики" всегда снаружи
        min_required_dist = self.main_radius + 10 + item_size
        self.current_submenu_radius = max(submenu_rad_config, min_required_dist)
        
        threshold_ratio = dir_cfg.get("threshold_ratio", DEFAULT_SUBMENU_CONFIG["threshold_ratio"])
        self.current_threshold = int(self.main_radius * threshold_ratio) 
        self.current_item_size = item_size
        
        # Окно уже перемещено в _on_direction_selected контроллера, просто показываем.
        self.show() 
        self._monitor_timer.start()
        
    def close_menu(self):
        self.active = False
        self.hide()
        self.current_direction = None
        self.highlight_index = None
        self.menu_data = {}
        self._last_selected_direction = None
        self.current_submenu_radius = 0
        self.current_threshold = 0
        self.current_item_size = 0
        self._monitor_timer.stop()
        self._hide_tooltip()
        self.preview_direction = None
        
        # Восстанавливаем игнорирование событий мыши
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.update()

    def mouseMoveEvent(self, event):
        """Обрабатывает перемещение мыши для обновления выделения и тултипов."""
        if not self.active or self.menu_level != 1:
            return
            
        # mx, my - координаты относительно окна 500x500
        mx, my = event.pos().x(), event.pos().y()
        
        # Логика для menu_level 1 (Submenu) - Наведение мышью имеет приоритет
        items = self.menu_data 
        n = len(items)
        old_highlight_index = self.highlight_index
        mouse_over_index = None
        
        if n > 0:
            start_angle = -90 
            item_radius = self.current_item_size 
            
            for i in range(n):
                angle_deg = start_angle + (360 / n) * i
                angle = math.radians(angle_deg)
                
                # В Submenu, центр - это self.center_x/y (250, 250)
                px = self.center_x + math.cos(angle) * self.current_submenu_radius
                py = self.center_y + math.sin(angle) * self.current_submenu_radius
                d = math.hypot(mx - px, my - py)
                
                if d < item_radius + 6: 
                    mouse_over_index = i
                    break
        
        # Если мышь наведена на элемент, устанавливаем его как выделенный
        if mouse_over_index is not None:
            self.highlight_index = mouse_over_index
        elif self.highlight_index is None and n > 0:
            # Если мышь не наведена ни на что, но элементы есть, сбрасываем highlight_index на 0
            # для удобства навигации колесом
            self.highlight_index = 0

        # Обновление тултипа
        if self.highlight_index is not None and self.highlight_index != old_highlight_index:
            self._update_tooltip_for_highlighted_item()
        elif self.highlight_index is None and old_highlight_index is not None:
            self._hide_tooltip()
            
        self.update() 
        
    def wheelEvent(self, event: QtGui.QWheelEvent):
        """Обрабатывает прокрутку колеса мыши для навигации по подменю."""
        if not self.active or self.menu_level != 1:
            super().wheelEvent(event)
            return

        items = self.menu_data
        n = len(items)
        if n == 0 or self.highlight_index is None:
            return

        # numDegrees() для точной прокрутки, < 0 для прокрутки вверх, > 0 для прокрутки вниз
        degrees = event.angleDelta().y()

        if degrees != 0:
            old_highlight_index = self.highlight_index
            
            # Прокрутка вниз (degrees > 0) -> по часовой стрелке (увеличение индекса)
            if degrees > 0: 
                self.highlight_index = (self.highlight_index + 1) % n
            # Прокрутка вверх (degrees < 0) -> против часовой стрелки (уменьшение индекса)
            else: 
                self.highlight_index = (self.highlight_index - 1 + n) % n

            if self.highlight_index != old_highlight_index:
                self._update_tooltip_for_highlighted_item()
                self.update()
                
    def _update_tooltip_for_highlighted_item(self):
        """Обновляет тултип для текущего выделенного элемента."""
        if self.highlight_index is not None:
            selected_item = self.menu_data[self.highlight_index]
            label = selected_item.get('label', '')
            keys = selected_item.get('keys', '')
            
            tooltip_text = f"**{label}**"
            if keys and selected_item.get('type') != 'text':
                tooltip_text += f"\nHotkey: {keys}"
            if selected_item.get('type') in ('text', 'hotkey_and_text'):
                value = selected_item.get('value', '').strip()
                if len(value) > 0:
                    tooltip_text += "\nText Action:\n" + value
                    
            self._show_tooltip(tooltip_text)
        else:
             self._hide_tooltip()

    def paintEvent(self, event):
        if not self.active:
            return
            
        # Курсор в мировых (глобальных) координатах
        pos = QtGui.QCursor.pos() 
        global_mx, global_my = pos.x(), pos.y()
        
        # Глобальные координаты центра меню (для расчета расстояния/угла)
        global_center_x = self.x() + self.center_x
        global_center_y = self.y() + self.center_y
        
        # Курсор относительно центра МЕНЮ (для расчета расстояния/угла)
        dx = global_mx - global_center_x
        dy = global_my - global_center_y
        dist = math.hypot(dx, dy)
        
        # --- SELECTION LOGIC for Menu Level 0 (Main Menu) ---
        current_preview_direction = None
        if self.menu_level == 0:
            
            # Логика определения направления (для превью и активации)
            if dist > self.main_radius * 0.5: # 50% Radius for Preview
                
                angle = math.degrees(math.atan2(dy, dx))
                if angle < 0: angle += 360

                directions = [('east', 0), ('south', 90), ('west', 180), ('north', 270)]
                
                min_diff = 360
                closest_direction = None
                
                # Находим ближайшее направление
                for d, target_angle in directions:
                    diff = abs(angle - target_angle)
                    diff = min(diff, 360 - diff) 
                    
                    if diff < min_diff:
                        min_diff = diff
                        closest_direction = d
                
                MAX_ANGLE_DIFF = 45 
                
                if min_diff < MAX_ANGLE_DIFF:
                     current_preview_direction = closest_direction
                     
                
            # Обновление текущего направления для отрисовки превью
            self.preview_direction = current_preview_direction 
            
            # ВАЖНО: Мы переключаемся на submenu, если dist > main_radius.
            if dist > self.main_radius:
                if self.current_direction != current_preview_direction and current_preview_direction:
                    self.current_direction = current_preview_direction
                    # Отправка сигнала для переключения в RadialController
                    self.direction_passed_threshold.emit(self.current_direction) 
            else:
                 self.current_direction = None

        # --- DRAWING LOGIC ---
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.Antialiasing)

        qp.setPen(QtCore.Qt.NoPen)
        qp.setBrush(QtGui.QBrush(QtGui.QColor(0,0,0,0)))
        qp.drawRect(self.rect())

        base_center = QtCore.QPoint(self.center_x, self.center_y) # Локальный центр (250, 250)
        
        # --- 1. Draw Mouse Line (Only Menu Level 0) ---
        if self.menu_level == 0:
            if dist > 0:
                # Координаты курсора относительно ОКНА (не относительно центра)
                relative_mx = global_mx - self.x() 
                relative_my = global_my - self.y() 

                line_pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 150))
                line_pen.setWidth(2)
                qp.setPen(line_pen)
                # Линия от локального центра до курсора (относительно окна)
                qp.drawLine(self.center_x, self.center_y, relative_mx, relative_my)
        
        # --- 2. Draw Main Wheel ---
        
        if self.menu_level == 0:
            # Полупрозрачное главное меню
            bg_alpha = 150 
            outline_color = QtGui.QColor(180, 20, 20, bg_alpha + 50)
        else: # menu_level 1
            bg_alpha = 220
            outline_color = self.SUBMENU_COLORS.get(self.current_direction, QtGui.QColor(180, 180, 180, bg_alpha + 30))
            outline_color.setAlpha(bg_alpha + 30)
            
            if self.highlight_index is not None:
                outline_color = outline_color.lighter(120)
                outline_color.setAlpha(255) 

        # Внешняя граница
        pen = QtGui.QPen(outline_color)
        pen.setWidth(4)
        qp.setPen(pen)
        qp.setBrush(QtGui.QBrush(QtGui.QColor(0,0,0, bg_alpha)))
        qp.drawEllipse(base_center, self.main_radius + 10, self.main_radius + 10) 
        
        # Внутренний круг
        qp.setPen(QtCore.Qt.NoPen)
        qp.setBrush(QtGui.QBrush(QtGui.QColor(20,20,20, bg_alpha)))
        qp.drawEllipse(base_center, self.main_radius, self.main_radius) 

        # --- 3. Draw Direction Labels (Menu Level 0) ---
        if self.menu_level == 0:
            dir_vec = {
                'north': (0, -1), 'east': (1, 0), 'south': (0, 1), 'west': (-1, 0)
            }
            LABEL_PADDING = 30 
            label_offset = self.main_radius + LABEL_PADDING 
            
            for d, (vx, vy) in dir_vec.items():
                px = int(self.center_x + vx * label_offset)
                py = int(self.center_y + vy * label_offset)
                
                is_preview = d == self.preview_direction
                
                if is_preview:
                    brush_color = self.SUBMENU_COLORS.get(d, QtGui.QColor(200, 20, 20, 230))
                    brush = QtGui.QBrush(brush_color)
                else:
                    brush = QtGui.QBrush(QtGui.QColor(30, 30, 30, 220))
                
                qp.setBrush(brush)
                qp.setPen(QtCore.Qt.NoPen)
                rect = QtCore.QRect(px-50, py-16, 100, 32)
                qp.drawRoundedRect(rect, 10, 10)
                
                qp.setPen(QtGui.QPen(QtGui.QColor(255,255,255,230)))
                font = QtGui.QFont("Sans", 9, QtGui.QFont.Bold if is_preview else QtGui.QFont.Normal)
                qp.setFont(font)
                label = self.cfg.get("directions", {}).get(d, {}).get("label", d.capitalize())
                qp.drawText(rect, QtCore.Qt.AlignCenter, label)

        # --- 4. Draw Submenu Items (Menu Level 1) ---
        elif self.menu_level == 1:
            items = self.menu_data 
            n = len(items)
            item_radius = self.current_item_size
            
            submenu_highlight_color = self.SUBMENU_COLORS.get(self.current_direction, QtGui.QColor(35, 35, 35, 255))
            
            if n == 0:
                qp.setPen(QtGui.QPen(QtGui.QColor(180,180,180,200)))
                qp.setFont(QtGui.QFont("Sans", 9))
                qp.drawText(QtCore.QRect(self.center_x-100, self.center_y-12, 200, 24), QtCore.Qt.AlignCenter, "No actions assigned")
            else:
                start_angle = -90 
                for i, it in enumerate(items):
                    angle_deg = start_angle + (360 / n) * i
                    angle = math.radians(angle_deg)
                    
                    px = int(self.center_x + math.cos(angle) * self.current_submenu_radius) 
                    py = int(self.center_y + math.sin(angle) * self.current_submenu_radius)
                    center_pt = QtCore.QPoint(px, py)
                    
                    
                    if self.highlight_index == i:
                        brush = QtGui.QBrush(submenu_highlight_color) 
                        pen_color = QtGui.QColor(255,255,255,255) 
                    else:
                        brush = QtGui.QBrush(QtGui.QColor(35, 35, 35, 255)) 
                        pen_color = QtGui.QColor(255,255,255,230) 
                        
                    qp.setBrush(brush)
                    qp.setPen(QtCore.Qt.NoPen)
                    qp.drawEllipse(center_pt, item_radius, item_radius) 
                    
                    qp.setPen(QtGui.QPen(pen_color))
                    qp.setFont(QtGui.QFont("Sans", 8))
                    
                    text_rect_width = int(item_radius * 2 * 0.9)
                    text_rect_height = int(item_radius * 2 * 0.6)
                    label_text = it.get('label','')
                    if self.highlight_index != i:
                        label_text = label_text[:5] + "..." if len(label_text) > 5 else label_text
                        
                    qp.drawText(QtCore.QRect(px - text_rect_width//2, py - text_rect_height//2, text_rect_width, text_rect_height), QtCore.Qt.AlignCenter, label_text)


    def get_selection(self) -> Optional[Dict]:
        """Returns the final selection based on the current state (only Level 1 selection is returned)."""
        if self.menu_level == 0 or self.highlight_index is None:
            return None
        
        items = self.menu_data 
        idx = self.highlight_index
        if idx < 0 or idx >= len(items):
            return None
        
        return {"direction": self.current_direction, "index": idx, "item": items[idx]}

# ------------------------------
# Захват хоткея (без изменений)
# ------------------------------
class HotkeyCaptureDialog(QtWidgets.QDialog):
    
    capture_finished = QtCore.pyqtSignal()
    
    def __init__(self, parent=None, single_key_mode=False):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() & ~QtCore.Qt.WindowCloseButtonHint) 
        self.setWindowTitle("Press and Release hotkey (Esc to cancel)")
        self.setFixedSize(420, 80)
        layout = QtWidgets.QVBoxLayout(self)
        
        self.label = QtWidgets.QLabel(
            "Press and RELEASE your desired hotkey/combo (e.g., Shift+1, Ctrl+C). Press ESC to cancel.", self
        )
        layout.addWidget(self.label)
        
        self.result = None
        self._capture_running = True 
        
        self.single_key_mode = single_key_mode 
        
        self._current_keys = set()
        self._pressed_order = [] 
        
        self._thread = threading.Thread(target=self._capture_loop)
        self._thread.daemon = True
        self.capture_finished.connect(self.accept)
        self._thread.start()

    def _capture_loop(self):
        """Мониторит ввод и захватывает хоткей при отпускании."""
        
        mouse_buttons = {
            "x": "mouse x1",      
            "x2": "mouse x2",     
            "left": "mouse left",
            "right": "mouse right",
            "middle": "mouse middle",
        }
        
        keyboard.hook(self._keyboard_event_handler)

        try:
            while self._capture_running:
                
                # 1. Проверка Esc (отмена)
                if keyboard.is_pressed("esc"):
                    self.result = None
                    self._capture_running = False
                    break
                    
                # 2. Проверка мыши (захват при НАЖАТИИ)
                for btn_key, btn_name in mouse_buttons.items():
                    if mouse.is_pressed(btn_key):
                        if not self._pressed_order: 
                            self.result = btn_name
                            self._capture_running = False 
                            break

                if not self._capture_running and self.result:
                    break
                
                time.sleep(0.01)

        except Exception as e:
            print("Hotkey capture error:", e)
            self.result = None
        finally:
            keyboard.unhook_all()
            self.capture_finished.emit()

    def _get_base_key_name(self, event) -> Optional[str]:
        """Пытается получить истинное имя клавиши (например, '1' вместо '!') по scan_code."""
        
        if hasattr(event, 'button'): 
            return None 

        try:
            base_name = keyboard.normalize_name(event.scan_code)
            return base_name
        except Exception:
            return event.name

    def _normalize_key_name(self, key_name: str) -> str:
        """Нормализует имена клавиш."""
        key_name = key_name.lower()
        
        if key_name in ('caps lock', 'scroll lock', 'num lock', 'win'):
            return None
            
        if key_name.endswith(' shift'): return 'shift'
        if key_name == 'shift': return 'shift'

        if key_name.endswith(' control'): return 'ctrl'
        if key_name == 'control': return 'ctrl'
        
        if key_name.endswith(' alt'): return 'alt'
        if key_name == 'alt': return 'alt'
        
        if key_name in ('lcontrol', 'rcontrol'): return 'ctrl'
        if key_name in ('lshift', 'rshift'): return 'shift'
        if key_name in ('lmenu', 'rmenu'): return 'alt'
        
        return key_name

    def _keyboard_event_handler(self, event):
        """Обрабатывает события клавиатуры."""
        if not self._capture_running or self.result is not None:
            return

        key_name_base = self._get_base_key_name(event)
        
        if key_name_base is None:
            return

        normalized_name = self._normalize_key_name(key_name_base)
        
        if normalized_name is None:
            return
            
        if event.event_type == 'down':
            if normalized_name not in self._current_keys:
                self._current_keys.add(normalized_name)
                self._pressed_order.append(normalized_name)
            
            if self.single_key_mode:
                self.result = normalized_name
                self._capture_running = False
                return
            
        elif event.event_type == 'up':
            
            if normalized_name in self._current_keys:
                self._current_keys.remove(normalized_name)
            
            if not self.single_key_mode and not self._current_keys and self._pressed_order:
                
                if len(self._pressed_order) == 1 and self._pressed_order[0] in ('shift', 'ctrl', 'alt'):
                    self._pressed_order = []
                    return
                
                self.result = "+".join(self._pressed_order)
                
                self._capture_running = False
                
    def reject(self):
        """Корректное завершение при отмене (нажатии Esc)."""
        self._capture_running = False 
        self.result = None 
        super().reject()

    def closeEvent(self, event):
        """Обеспечение остановки потока при закрытии окна."""
        self._capture_running = False
        event.accept()

# ------------------------------
# Окно настроек (без изменений, кроме вызова сохранения)
# ------------------------------
class SettingsWindow(QtWidgets.QWidget):
    
    config_saved = QtCore.pyqtSignal() # НОВЫЙ СИГНАЛ
    
    def __init__(self, cfg: Dict, save_callback=None):
        super().__init__()
        self.setWindowTitle("Radial Menu — Settings")
        self.cfg = cfg
        self.save_callback = save_callback
        self.resize(850, 680) 
        v = QtWidgets.QVBoxLayout(self)

        # -------------------
        # 1. Activation Settings
        # -------------------
        act_box = QtWidgets.QGroupBox("Activation (hold to open)")
        hv = QtWidgets.QHBoxLayout(act_box)
        hv.addWidget(QtWidgets.QLabel("Hotkey Combo:"))
        
        current_combo = self.cfg.get("activation",{}).get("combo", DEFAULT_CONFIG["activation"]["combo"])
        
        self.combo_edit = QtWidgets.QLineEdit(current_combo)
        self.combo_edit.setFixedWidth(160)
        hv.addWidget(self.combo_edit)
        
        self.capture_act_btn = QtWidgets.QPushButton("Record activation (press combo)")
        hv.addWidget(self.capture_act_btn)
        hv.addStretch()
        v.addWidget(act_box)
        self.capture_act_btn.clicked.connect(self._capture_activation)

        # -------------------
        # 2. Global Visual Settings
        # -------------------
        vis_box = QtWidgets.QGroupBox("Global Visual Settings")
        hv_vis = QtWidgets.QHBoxLayout(vis_box)
        vis_cfg = self.cfg.get("visual", DEFAULT_CONFIG["visual"])

        # Main Menu Radius 
        hv_vis.addWidget(QtWidgets.QLabel("Main Menu/Threshold Radius (px):"))
        self.main_radius_edit = QtWidgets.QLineEdit(str(vis_cfg.get("main_radius", DEFAULT_CONFIG["visual"]["main_radius"])))
        self.main_radius_edit.setFixedWidth(50)
        self.main_radius_edit.setValidator(QtGui.QIntValidator(10, 500))
        hv_vis.addWidget(self.main_radius_edit)
        
        hv_vis.addStretch()
        v.addWidget(vis_box)

        # -------------------
        # 3. Directions Settings (Per-Submenu Settings)
        # -------------------
        dirs_box = QtWidgets.QGroupBox("Directions and their items (max 9 each) & Per-Submenu Visuals")
        self.main_grid = QtWidgets.QGridLayout(dirs_box)
        dirs = ["north","east","south","west"]
        self.dir_name_edits: Dict[str, QtWidgets.QLineEdit] = {}
        self.items_lists: Dict[str, QtWidgets.QListWidget] = {}
        
        self.submenu_radius_edits: Dict[str, QtWidgets.QLineEdit] = {}
        self.threshold_ratio_edits: Dict[str, QtWidgets.QLineEdit] = {}
        self.item_size_edits: Dict[str, QtWidgets.QLineEdit] = {}
        
        for i, d in enumerate(dirs):
            dir_cfg = self.cfg["directions"][d]
            
            # --- Row 1: Direction Label & Per-Submenu Visuals ---
            row_idx = i * 3
            
            # Direction Label
            self.main_grid.addWidget(QtWidgets.QLabel(d.upper()), row_idx, 0)
            name = QtWidgets.QLineEdit(dir_cfg.get("label", d.capitalize()))
            self.dir_name_edits[d] = name
            self.main_grid.addWidget(name, row_idx, 1)
            
            # Submenu Radius 
            sr_label = QtWidgets.QLabel("Submenu Dist (px):")
            self.main_grid.addWidget(sr_label, row_idx, 2)
            sr_edit = QtWidgets.QLineEdit(str(dir_cfg.get("submenu_radius", DEFAULT_SUBMENU_CONFIG["submenu_radius"])))
            sr_edit.setFixedWidth(50)
            sr_edit.setValidator(QtGui.QIntValidator(20, 1000))
            self.submenu_radius_edits[d] = sr_edit
            self.main_grid.addWidget(sr_edit, row_idx, 3)

            # Threshold Ratio (Now in Percentages)
            tr_label = QtWidgets.QLabel("Threshold % (10.0-100.0):")
            self.main_grid.addWidget(tr_label, row_idx, 4)
            # Отображаем как процент (умножаем на 100)
            tr_value = dir_cfg.get('threshold_ratio', DEFAULT_SUBMENU_CONFIG['threshold_ratio']) * 100.0
            # Форматируем с точкой
            tr_edit = QtWidgets.QLineEdit(f"{tr_value:.2f}")
            tr_edit.setFixedWidth(70)
            
            # ИСПРАВЛЕНИЕ: Валидатор для корректного ввода чисел с точкой 
            ratio_validator = QtGui.QDoubleValidator(10.0, 100.0, 2, self)
            ratio_validator.setNotation(QtGui.QDoubleValidator.StandardNotation)
            # Принудительное использование точки как разделителя
            ratio_validator.setLocale(QtCore.QLocale(QtCore.QLocale.C)) 
            tr_edit.setValidator(ratio_validator)
            
            self.threshold_ratio_edits[d] = tr_edit
            self.main_grid.addWidget(tr_edit, row_idx, 5)
            
            # Item Size 
            is_label = QtWidgets.QLabel("Item Size (Radius, px):")
            self.main_grid.addWidget(is_label, row_idx, 6)
            is_edit = QtWidgets.QLineEdit(str(dir_cfg.get("item_size", DEFAULT_SUBMENU_CONFIG["item_size"])))
            is_edit.setFixedWidth(50)
            is_edit.setValidator(QtGui.QIntValidator(10, 100))
            self.item_size_edits[d] = is_edit
            self.main_grid.addWidget(is_edit, row_idx, 7)
            
            # --- Row 2: Item List and Buttons ---
            
            listw = QtWidgets.QListWidget()
            listw.setFixedHeight(140)
            listw.setFixedWidth(380)
            # Drag & Drop for reordering
            listw.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
            listw.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            
            for it in dir_cfg.get("items", []):
                li = QtWidgets.QListWidgetItem(self._format_item_text(it))
                li.setData(QtCore.Qt.UserRole, it)
                listw.addItem(li)
            self.items_lists[d] = listw
            self.main_grid.addWidget(listw, row_idx + 1, 2, 2, 3) # Span across list and buttons columns
            
            # Набор кнопок для управления элементами
            btns = QtWidgets.QVBoxLayout()
            add_hk_btn = QtWidgets.QPushButton("Add Hotkey")
            add_text_btn = QtWidgets.QPushButton("Add Text") 
            add_hk_text_btn = QtWidgets.QPushButton("Add Hotkey + Text") 
            rename_btn = QtWidgets.QPushButton("Rename Selected")
            reassign_btn = QtWidgets.QPushButton("Reassign") 
            rem_btn = QtWidgets.QPushButton("Remove Selected")
            
            btns.addWidget(add_hk_btn)
            btns.addWidget(add_text_btn)
            btns.addWidget(add_hk_text_btn) 
            btns.addWidget(rename_btn)
            btns.addWidget(reassign_btn)
            btns.addStretch(1) 
            btns.addWidget(rem_btn)
            
            self.main_grid.addLayout(btns, row_idx + 1, 5, 2, 1)
            
            # Add a vertical separator line (optional, for visual clarity)
            line = QtWidgets.QFrame()
            line.setFrameShape(QtWidgets.QFrame.HLine)
            line.setFrameShadow(QtWidgets.QFrame.Sunken)
            self.main_grid.addWidget(line, row_idx + 2, 0, 1, 8) 
            
            # Привязка
            add_hk_btn.clicked.connect(lambda _, dd=d: self._add_hotkey_item(dd))
            add_text_btn.clicked.connect(lambda _, dd=d: self._add_text_item(dd))
            add_hk_text_btn.clicked.connect(lambda _, dd=d: self._add_hotkey_text_item(dd)) 
            rem_btn.clicked.connect(lambda _, dd=d: self._remove_item(dd))
            rename_btn.clicked.connect(lambda _, dd=d: self._rename_item(dd))
            reassign_btn.clicked.connect(lambda _, dd=d: self._reassign_item(dd))
            

        v.addWidget(dirs_box)

        # -------------------
        # 4. Save/Cancel
        # -------------------
        hb = QtWidgets.QHBoxLayout()
        hb.addStretch()
        save_btn = QtWidgets.QPushButton("Save")
        cancel_btn = QtWidgets.QPushButton("Cancel")
        hb.addWidget(save_btn)
        hb.addWidget(cancel_btn)
        save_btn.clicked.connect(self._save)
        cancel_btn.clicked.connect(self.close)
        v.addLayout(hb)
        
    def _format_item_text(self, item_data: Dict) -> str:
        """Форматирует текст элемента для QListWidget."""
        label = item_data.get("label", "")
        item_type = item_data.get("type", "hotkey")
        
        if item_type == "text":
            value = item_data.get("value", "")
            display_value = value.replace('\n', ' ')
            display_value = display_value[:20] + "..." if len(display_value) > 20 else display_value
            return f'{label}    [Text: "{display_value}"]'
        
        elif item_type == "hotkey_and_text":
            keys = item_data.get("keys", "")
            value = item_data.get("value", "")
            display_value = value.replace('\n', ' ')
            display_value = display_value[:10] + "..." if len(display_value) > 10 else display_value
            return f'{label}    [{keys} + Text: "{display_value}"]'
        
        else: # hotkey
            keys = item_data.get("keys", "")
            return f'{label}    [{keys}]'

    def _update_list_item(self, list_item: QtWidgets.QListWidgetItem, item_data: Dict):
        """Вспомогательный метод для обновления текста элемента списка и его данных."""
        list_item.setText(self._format_item_text(item_data))
        list_item.setData(QtCore.Qt.UserRole, item_data)

    def _capture_activation(self):
        dlg = HotkeyCaptureDialog(self, single_key_mode=False) 
        result_code = dlg.exec_() 
        if result_code == QtWidgets.QDialog.Accepted and dlg.result:
            self.combo_edit.setText(dlg.result)
            
    def _add_hotkey_item(self, direction):
        lw = self._check_limit_and_get_list(direction)
        if lw is None: return
            
        text, ok = QtWidgets.QInputDialog.getText(self, "New Hotkey Action", "Enter the label for the hotkey action:")
        if not ok or not text.strip(): return
        label = text.strip()

        final_keys = self._get_two_part_hotkey(label)
        if final_keys is None: return

        item = {"label": label, "keys": final_keys, "type": "hotkey"}
        li = QtWidgets.QListWidgetItem(self._format_item_text(item))
        li.setData(QtCore.Qt.UserRole, item)
        lw.addItem(li)
        
    def _add_text_item(self, direction):
        lw = self._check_limit_and_get_list(direction)
        if lw is None: return
            
        text_label, ok = QtWidgets.QInputDialog.getText(self, "New Text Action", "Enter the label for the text action:")
        if not ok or not text_label.strip(): return
        label = text_label.strip()

        text_value, ok = QtWidgets.QInputDialog.getMultiLineText(self, "Text Content", f"Enter the text to be typed when '{label}' is selected:")
        if not ok: return

        item = {"label": label, "type": "text", "value": text_value}
        li = QtWidgets.QListWidgetItem(self._format_item_text(item))
        li.setData(QtCore.Qt.UserRole, item)
        lw.addItem(li)
        
    def _add_hotkey_text_item(self, direction):
        lw = self._check_limit_and_get_list(direction)
        if lw is None: return
            
        text_label, ok = QtWidgets.QInputDialog.getText(self, "New Hotkey + Text Action", "Enter the label for the action:")
        if not ok or not text_label.strip(): return
        label = text_label.strip()

        final_keys = self._get_two_part_hotkey(label)
        if final_keys is None: return
        
        text_value, ok = QtWidgets.QInputDialog.getMultiLineText(self, "Text Content", f"Enter the text to be typed when '{label}' is selected (after hotkey):")
        if not ok: return

        item = {"label": label, "keys": final_keys, "type": "hotkey_and_text", "value": text_value}
        li = QtWidgets.QListWidgetItem(self._format_item_text(item))
        li.setData(QtCore.Qt.UserRole, item)
        lw.addItem(li)

    def _check_limit_and_get_list(self, direction):
        lw = self.items_lists[direction]
        if lw.count() >= 9:
            QtWidgets.QMessageBox.warning(self, "Limit", "Max 9 items per direction")
            return None
        return lw
        
    def _get_two_part_hotkey(self, label: str) -> Optional[str]:
        """Вспомогательный метод для захвата хоткея в два этапа."""
        
        dlg1 = HotkeyCaptureDialog(self, single_key_mode=True)
        dlg1.setWindowTitle(f"Record KEY 1 for: {label}")
        dlg1.label.setText("Press the FIRST key (e.g., Shift, Ctrl, F1). Press ESC to cancel.")
        
        if dlg1.exec_() != QtWidgets.QDialog.Accepted: return None
        
        key1 = dlg1.result or ""
        if not key1: return None

        key2 = ""
        dlg2 = HotkeyCaptureDialog(self, single_key_mode=True)
        dlg2.setWindowTitle(f"Record KEY 2 for: {label}")
        dlg2.label.setText(f"Press the SECOND key (or ESC for just '{key1}').")
        
        if dlg2.exec_() == QtWidgets.QDialog.Accepted and dlg2.result:
            key2 = dlg2.result
        
        return f"{key1}+{key2}" if key2 else key1


    def _remove_item(self, direction):
        lw = self.items_lists[direction]
        cur = lw.currentItem()
        if cur:
            lw.takeItem(lw.row(cur))

    def _rename_item(self, direction):
        lw = self.items_lists[direction]
        cur = lw.currentItem()
        if not cur:
            QtWidgets.QMessageBox.information(self, "Select", "Choose an item to rename")
            return
            
        it = cur.data(QtCore.Qt.UserRole)
        newlab, ok = QtWidgets.QInputDialog.getText(self, "Rename Label", "Label:", text=it.get("label",""))
        if not ok or not newlab.strip():
            return
            
        it["label"] = newlab.strip()
        self._update_list_item(cur, it)

    def _reassign_item(self, direction):
        lw = self.items_lists[direction]
        cur = lw.currentItem()
        if not cur:
            QtWidgets.QMessageBox.information(self, "Select", "Choose an item to reassign")
            return
            
        it = cur.data(QtCore.Qt.UserRole)
        current_label = it.get("label", "Action")
        item_type = it.get("type", "hotkey")

        if item_type == "text" or item_type == "hotkey_and_text": 
            text_value, ok = QtWidgets.QInputDialog.getMultiLineText(self, "Edit Text Content", f"Enter the new text for '{current_label}':", text=it.get("value", ""))
            if not ok:
                return
            it["value"] = text_value
            
            if item_type == "hotkey_and_text":
                res = QtWidgets.QMessageBox.question(self, "Hotkey", "Do you want to reassign the hotkey part as well?", QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No | QtWidgets.QMessageBox.Cancel)
                if res == QtWidgets.QMessageBox.Cancel:
                    return
                elif res == QtWidgets.QMessageBox.Yes:
                    self._reassign_hotkey_only(it, current_label)
                    
            self._update_list_item(cur, it)
            
        else: # hotkey
            self._reassign_hotkey_only(it, current_label)
            self._update_list_item(cur, it)
            
    def _reassign_hotkey_only(self, item_data: Dict, label: str):
        """Вспомогательный метод для переназначения только хоткея."""
        final_keys = self._get_two_part_hotkey(label)
        if final_keys is None:
            return
        item_data["keys"] = final_keys
        
    def _save(self):
        new_combo = self.combo_edit.text().strip().lower()
        if not new_combo:
            QtWidgets.QMessageBox.warning(self, "Error", "Activation hotkey combo cannot be empty.")
            return

        self.cfg["activation"]["combo"] = new_combo
        self.cfg["activation"].pop("modifier", None)
        self.cfg["activation"].pop("key", None)

        try:
            new_main_radius = int(self.main_radius_edit.text())
            self.cfg["visual"]["main_radius"] = max(10, new_main_radius)

        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Error", "Global visual settings must be valid numbers.")
            return

        for d, edit in self.dir_name_edits.items():
            
            # 1. Update Direction Label
            self.cfg["directions"][d]["label"] = edit.text().strip() or d.capitalize()
            
            # 2. Update Per-Submenu Visuals 
            try:
                new_submenu_radius = int(self.submenu_radius_edits[d].text())
                
                # Считываем как процент и переводим в ratio
                # Используем .text().replace(',', '.') для надежного парсинга
                threshold_text = self.threshold_ratio_edits[d].text().replace(',', '.')
                new_threshold_percent = float(threshold_text)
                
                new_threshold_ratio = new_threshold_percent / 100.0
                
                new_item_size = int(self.item_size_edits[d].text())
                
                self.cfg["directions"][d]["submenu_radius"] = max(20, new_submenu_radius)
                self.cfg["directions"][d]["threshold_ratio"] = max(0.1, min(1.0, new_threshold_ratio)) # Ограничение 0.1 до 1.0
                self.cfg["directions"][d]["item_size"] = max(10, min(100, new_item_size))
                
            except ValueError:
                QtWidgets.QMessageBox.warning(self, "Error", f"Visual settings for {d.upper()} must be valid numbers.")
                return

            # 3. Update Items
            lw = self.items_lists[d]
            new_items = []
            for i in range(lw.count()):
                it = lw.item(i).data(QtCore.Qt.UserRole)
                if it:
                    new_items.append(it)
            self.cfg["directions"][d]["items"] = new_items
        
        save_config(self.cfg)
        
        # ОТПРАВКА СИГНАЛА об успешном сохранении
        self.config_saved.emit()
        
        if self.save_callback:
            self.save_callback()
            
        QtWidgets.QMessageBox.information(self, "Saved", f"Saved to {CONFIG_PATH}")
        self.close()

# ------------------------------
# Контроллер (обновлён для горячей перезагрузки конфигурации)
# ------------------------------
class RadialController(QtCore.QObject):
    
    activation_started = QtCore.pyqtSignal(int, int)  
    activation_ended = QtCore.pyqtSignal()
    
    _MAX_DEBOUNCE = 3 

    def __init__(self, cfg: Dict, overlay: RadialOverlay):
        super().__init__()
        self.cfg = cfg
        self.overlay = overlay
        
        self.activation_combo = self.cfg.get("activation", {}).get("combo", "alt+x").lower()
        
        self._active = False
        self._menu_level = 0 
        self._current_direction = None
        self._active_debounce = 0 
        
        # Глобальные координаты центра, где было открыто ГЛАВНОЕ меню
        self._initial_center_x = 0 
        self._initial_center_y = 0
        
        self.activation_started.connect(self._on_activation_started)
        self.activation_ended.connect(self._on_activation_ended)
        self.overlay.direction_passed_threshold.connect(self._on_direction_selected)

        self._monitor_timer = QtCore.QTimer(self)
        self._monitor_timer.timeout.connect(self._check_activation_state)
        
        self._update_config_dependent_state(cfg) # Инициализация

    def _update_config_dependent_state(self, new_cfg: Dict):
        """Обновляет состояние контроллера на основе новой конфигурации."""
        self.cfg = new_cfg
        self.activation_combo = self.cfg.get("activation", {}).get("combo", DEFAULT_CONFIG["activation"]["combo"]).lower()
        
        # Обновление таймера
        interval = self.cfg.get("visual", {}).get("timer_interval_ms", DEFAULT_CONFIG["visual"]["timer_interval_ms"]) 
        if self._monitor_timer.interval() != interval:
            self._monitor_timer.stop()
            self._monitor_timer.setInterval(interval) 
        
        if not self._monitor_timer.isActive():
             self._monitor_timer.start()

    def _is_activation_active(self) -> bool:
        combo = self.activation_combo.strip().lower()
        try:
            if combo in ("mouse x1", "x1", "mouse_x1"):
                return mouse.is_pressed(button="x")  
            elif combo in ("mouse x2", "x2", "mouse_x2"):
                return mouse.is_pressed(button="x2")  
            elif combo.startswith("mouse "):
                btn = combo.split("mouse ", 1)[1]
                return mouse.is_pressed(button=btn)
            else:
                return keyboard.is_pressed(combo)
        except Exception:
            return False

    def _check_activation_state(self):
        active_now = self._is_activation_active()
        
        if active_now:
            self._active_debounce = self._MAX_DEBOUNCE 
            if not self._active:
                pos = QtGui.QCursor.pos()
                self._active = True 
                self.activation_started.emit(int(pos.x()), int(pos.y()))
            
        else:
            if self._active_debounce > 0:
                self._active_debounce -= 1
            else:
                if self._active:
                    self.activation_ended.emit()
            
    @QtCore.pyqtSlot(int, int)
    def _on_activation_started(self, x, y):
        self._menu_level = 0
        self._current_direction = None
        # Сохраняем начальный центр ГЛАВНОГО меню
        self._initial_center_x = x
        self._initial_center_y = y
        # open_main_menu сам перемещает окно на (x, y)
        self.overlay.open_main_menu(x, y)

    @QtCore.pyqtSlot(str)
    def _on_direction_selected(self, direction: str):
        # Эта функция вызывается, когда курсор пересек main_radius
        if self._menu_level == 0 and self._active:
            self._menu_level = 1
            self._current_direction = direction
            items = self.cfg['directions'][direction].get('items', [])
            
            # --- ИЗМЕНЕНИЕ: Расчет нового центра подменю ---
            
            main_radius = self.overlay.main_radius
            local_center_size = self.overlay.center_x # 250
            
            # 1. Определяем угол направления
            angle_map = {'east': 0, 'south': 90, 'west': 180, 'north': 270}
            angle_deg = angle_map.get(direction, 0)
            angle_rad = math.radians(angle_deg)
            
            # 2. Вычисляем ГЛОБАЛЬНЫЕ координаты точки перехода (новый центр)
            transition_x = int(self._initial_center_x + math.cos(angle_rad) * main_radius)
            transition_y = int(self._initial_center_y + math.sin(angle_rad) * main_radius)
            
            # 3. Вычисляем ГЛОБАЛЬНЫЕ координаты верхнего левого угла окна 500x500
            # так, чтобы transition_x/y были в локальном центре (250, 250)
            new_ul_x = transition_x - local_center_size
            new_ul_y = transition_y - local_center_size
            
            # 4. Перемещаем окно оверлея
            self.overlay.move(new_ul_x, new_ul_y)
            # --- КОНЕЦ ИЗМЕНЕНИЯ ---

            # Открываем подменю (которое теперь центрируется на точке перехода)
            self.overlay.open_submenu(direction, items)
    
    @QtCore.pyqtSlot()
    def _on_activation_ended(self):
        sel = self.overlay.get_selection()
        self.overlay.close_menu()
        self._active = False 
        self._menu_level = 0
        self._current_direction = None
        self._active_debounce = 0 
        
        # Сброс начальных координат
        self._initial_center_x = 0 
        self._initial_center_y = 0
        
        if sel:
            item = sel['item']
            item_type = item.get('type', 'hotkey') 
            
            if item_type == 'text':
                text_to_write = item.get('value', '')
                if text_to_write:
                    try:
                        keyboard.write(text_to_write)
                    except Exception as e:
                        print("Failed writing text:", e)
            
            elif item_type == 'hotkey_and_text': 
                seq = item.get('keys','')
                text_to_write = item.get('value', '')
                
                if seq:
                    try:
                        keyboard.send(seq)
                    except Exception as e:
                        print("Failed sending sequence:", seq, e)
                
                # --- ИЗМЕНЕНИЕ: Добавление задержки перед вводом текста (0.05 сек) ---
                if seq and text_to_write:
                    time.sleep(0.05) 
                # --- КОНЕЦ ИЗМЕНЕНИЯ ---
                        
                if text_to_write:
                    try:
                        keyboard.write(text_to_write)
                    except Exception as e:
                        print("Failed writing text:", e)
            
            else: # hotkey
                seq = item.get('keys','')
                if seq:
                    try:
                        keyboard.send(seq)
                    except Exception as e:
                        print("Failed sending sequence:", seq, e)

    def stop(self):
        self._monitor_timer.stop()

# ------------------------------
# Основной запуск (обновлён для работы в трее и горячей перезагрузки)
# ------------------------------
class ControlWidget(QtWidgets.QWidget):
    """Виджет, который ранее был ctrl, теперь используется только для окна настроек/закрытия."""
    def __init__(self, controller: RadialController, overlay: RadialOverlay, initial_config: Dict):
        super().__init__()
        self.setWindowTitle("Radial Menu — Control")
        self.setFixedSize(320, 140)
        self.controller = controller
        self.overlay = overlay
        self.cfg = initial_config
        
        layout = QtWidgets.QVBoxLayout(self)
        
        self.label = QtWidgets.QLabel(f"Radial Menu v1 — hold {self.controller.activation_combo} to open\nConfig: radial_config.json")
        self.label.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self.label)
        
        btn_layout = QtWidgets.QHBoxLayout()
        self.settings_btn = QtWidgets.QPushButton("Settings")
        self.quit_btn = QtWidgets.QPushButton("Quit")
        btn_layout.addWidget(self.settings_btn)
        btn_layout.addWidget(self.quit_btn)
        layout.addLayout(btn_layout)

        self.settings_btn.clicked.connect(self._open_settings)
        self.quit_btn.clicked.connect(self._quit_application)
        
    def _open_settings(self):
        self.settings_window = SettingsWindow(self.cfg.copy(), save_callback=self._update_controller_after_save)
        self.settings_window.config_saved.connect(self._update_controller_after_save)
        self.settings_window.show()
        
    @QtCore.pyqtSlot()
    def _update_controller_after_save(self):
        """Горячая перезагрузка конфигурации и обновление контроллера/оверлея."""
        global CONFIG
        
        # 1. Загрузка новой конфигурации
        new_cfg = load_config()
        CONFIG = new_cfg
        self.cfg = new_cfg
        
        # 2. Обновление контроллера
        self.controller._update_config_dependent_state(new_cfg)
        
        # 3. Обновление оверлея
        self.overlay.cfg = new_cfg 
        vis_cfg = new_cfg.get("visual", DEFAULT_CONFIG["visual"])
        self.overlay.main_radius = vis_cfg["main_radius"]
        
        north_cfg = new_cfg["directions"].get('north', DEFAULT_SUBMENU_CONFIG)
        self.overlay.current_threshold = int(self.overlay.main_radius * north_cfg.get("threshold_ratio", DEFAULT_SUBMENU_CONFIG["threshold_ratio"]))

        # 4. Обновление текста в окне управления (если оно открыто)
        self.label.setText(f"Radial Menu v1 — hold {self.controller.activation_combo} to open\nConfig: radial_config.json")
        
        if hasattr(self, 'tray_icon'):
            self.tray_icon.setToolTip(f"Radial Menu (Active)\nHotkey: {self.controller.activation_combo}")
        
    def _quit_application(self):
        self.controller.stop()
        QtWidgets.QApplication.quit()

def main():
    # Настройка для High DPI (важно для корректного отображения оверлея)
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    
    global CONFIG
    CONFIG = load_config()
    
    # Чтобы корректно работали тултипы
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps)
    
    app = QtWidgets.QApplication(sys.argv)
    
    # -------------------
    # Инициализация
    # -------------------
    overlay = RadialOverlay(CONFIG)
    overlay.hide()
    controller = RadialController(CONFIG, overlay)

    # Виджет управления (используется только для хранения функций настроек/выхода)
    control_widget = ControlWidget(controller, overlay, CONFIG)
    
    # -------------------
    # Системный Трей (Tray Icon)
    # -------------------
    
    # Создание иконки трея (потребуется иконка в .exe)
    # Если нет ico, можно использовать стандартную иконку или просто текст
    if Path(SCRIPT_DIR / "icon.ico").exists():
        icon = QtGui.QIcon(str(SCRIPT_DIR / "icon.ico"))
    else:
        # Запасной вариант - стандартная иконка
        icon = QtWidgets.QStyle.StandardPixmap.SP_DriveHDIcon
    
    tray_icon = QtWidgets.QSystemTrayIcon(app.style().standardIcon(icon), app)
    tray_icon.setToolTip(f"Radial Menu (Active)\nHotkey: {controller.activation_combo}")

    # Создание меню трея
    tray_menu = QtWidgets.QMenu()
    
    action_settings = tray_menu.addAction("Settings")
    action_settings.triggered.connect(control_widget.show) # Показать окно настроек
    
    action_quit = tray_menu.addAction("Quit")
    action_quit.triggered.connect(control_widget._quit_application)

    tray_icon.setContextMenu(tray_menu)
    tray_icon.show()
    
    # Сохраняем иконку трея в ControlWidget для возможного обновления тултипа
    control_widget.tray_icon = tray_icon 
    
    # Скрываем главное окно (оно больше не нужно)
    #QtWidgets.QApplication.setQuitOnLastWindowClosed(False)
    
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()