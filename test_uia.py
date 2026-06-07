import uiautomation as auto
import time
import os

try:
    import dxcam
    from PIL import Image
    camera = dxcam.create(output_idx=0, output_color="RGB")
    USE_DXCAM = True
    print("DXCAM initialized for DXGI screen capture.")
except ImportError:
    print("dxcam or PIL not installed. Please run: pip install dxcam pillow")
    USE_DXCAM = False

last_text = ""
last_control = ""

os.makedirs("screenshots", exist_ok=True)
def capture_screen(event_name="change", control=None):
    if not USE_DXCAM:
        return
        
    region = None
    if control:
        try:
            window = control.GetTopLevelControl()
            if window:
                rect = window.BoundingRectangle
                # DXCAM 截取区域 (left, top, right, bottom)
                left = max(0, rect.left)
                top = max(0, rect.top)
                right = min(camera.width, rect.right)
                bottom = min(camera.height, rect.bottom)
                if right > left and bottom > top:
                    region = (left, top, right, bottom)
        except Exception:
            pass

    # dxcam.grab() 配合 region 参数只截取目标窗口范围
    frame = camera.grab(region=region)
    if frame is not None:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"screenshots/{timestamp}_{event_name}.png"
        try:
            img = Image.fromarray(frame)
            img.save(filename)
            print(f"[*] Screen captured (DXGI): {filename}")
        except Exception as e:
            print(f"Error saving image: {e}")

print("Start monitoring focused UIA control...")
while True:
    try:
        control = auto.GetFocusedControl()
        if not control:
            time.sleep(0.1)
            continue
            
        ctrl_type = control.ControlTypeName
        ctrl_name = control.Name
        
        focus_id = f"[{ctrl_type}] {ctrl_name}"
        changed = False
        
        if focus_id != last_control:
            print(f"\n--- Focus Changed: {focus_id} ---")
            last_control = focus_id
            changed = True

        text = ""
        
        # 依次尝试更高级的文本提取接口（部分控件支持，部分不支持）
        if hasattr(control, 'GetValuePattern'):
            try:
                text = control.GetValuePattern().Value
            except Exception:
                pass
                
        if not text and hasattr(control, 'GetTextPattern'):
            try:
                text = control.GetTextPattern().DocumentRange.GetText(-1)
            except Exception:
                pass
                
        # 兜底：如果高级模式取不到，直接去读取控件的展示名 (Name)
        if not text:
            text = control.Name
            
        if text != last_text:
            print(f">> Text Content: {text[:200]}")
            last_text = text
            changed = True
            
        if changed:
            # 只在内容或者窗口切换时截图，将焦点控件传过去用于定位所属窗口
            capture_screen("event", control)
            
    except Exception as e:
        # 如果读取发生错误，防止它被默默吞掉
        print(f"Error capturing UIA: {e}")
        
    time.sleep(0.1)
