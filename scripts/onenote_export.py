#!/usr/bin/env python3
"""
OneNote Mac UI Automation: Export MFL Staff Notebook pages as PDFs
Python-based using osascript subprocess calls
"""

import subprocess
import time
import json
import os
import logging
from pathlib import Path

# Configuration
NOTEBOOK_NAME = "MFL Staff Notebook"
EXPORT_FOLDER = Path("/Volumes/GCH/Second Brain GCH Work /OneNote Exports/PDFs")
CHECKPOINT_FILE = Path("/Volumes/GCH/Second Brain GCH Work /OneNote Exports/.export_checkpoint.json")
LOG_FILE = Path("/Volumes/GCH/Second Brain GCH Work /OneNote Exports/export.log")

EXPORT_FOLDER.mkdir(parents=True, exist_ok=True)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def run_osascript(script: str) -> tuple:
    """Run an AppleScript command and return (success, output)."""
    try:
        result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=30)
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except Exception as e:
        return False, "", str(e)

def log_message(msg: str):
    logger.info(msg)

def sanitize_name(name: str) -> str:
    for char in ['/', ':', '*', '?', '"', '<', '>', '|', '\\']:
        name = name.replace(char, '-')
    return name

def save_checkpoint(section_idx: int, page_idx: int, page_name: str):
    data = {
        "sectionIndex": section_idx,
        "pageIndex": page_idx,
        "pageName": page_name,
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    }
    CHECKPOINT_FILE.write_text(json.dumps(data))

def load_checkpoint():
    if CHECKPOINT_FILE.exists():
        try:
            data = json.loads(CHECKPOINT_FILE.read_text())
            return data.get("sectionIndex", 0), data.get("pageIndex", 0)
        except Exception:
            pass
    return 0, 0

def click_menu_item(menu_path: str) -> bool:
    """Click a menu item given as 'Menu > Submenu > Item'."""
    parts = menu_path.split(' > ')
    if len(parts) < 2:
        return False
    
    menu_name = parts[0]
    item_name = parts[-1]
    
    script = f'''
    tell application "System Events"
        tell process "Microsoft OneNote"
            click menu item "{item_name}" of menu "{menu_name}" of menu bar 1
        end tell
    end tell
    '''
    success, _, err = run_osascript(script)
    if not success:
        logger.warning(f"Failed to click {menu_path}: {err}")
    return success

def click_button_in_window(button_name: str, window_title: str = "") -> bool:
    script = f'''
    tell application "System Events"
        tell process "Microsoft OneNote"
            try
                click button "{button_name}" of window "{window_title}"
                return true
            on error
                return false
            end try
        end tell
    end tell
    '''
    success, out, _ = run_osascript(script)
    return success and out.lower() == "true"

def keystroke_text(text: str) -> bool:
    script = f'''
    tell application "System Events"
        keystroke "{text}"
    end tell
    '''
    success, _, _ = run_osascript(script)
    return success

def keystroke_shortcut(keys: str) -> bool:
    """Send keystroke with modifiers. keys format: 'g|command,shift'"""
    parts = keys.split('|')
    key = parts[0]
    modifiers = parts[1].split(',') if len(parts) > 1 else []
    
    mod_str = ""
    if modifiers:
        mod_str = " using {" + ", ".join(modifiers) + "}"
    
    script = f'''
    tell application "System Events"
        keystroke "{key}"{mod_str}
    end tell
    '''
    success, _, _ = run_osascript(script)
    return success

def get_section_count() -> int:
    script = '''
    tell application "System Events"
        tell process "Microsoft OneNote"
            try
                set btns to buttons of toolbar 1 of window 1
                return (count of btns)
            on error
                return 0
            end try
        end tell
    end tell
    '''
    success, out, _ = run_osascript(script)
    try:
        return int(out.strip())
    except:
        return 0

def get_page_count() -> int:
    script = '''
    tell application "System Events"
        tell process "Microsoft OneNote"
            try
                set pgRows to rows of outline 1 of scroll area 1 of splitter group 1 of window 1
                return (count of pgRows)
            on error
                return 0
            end try
        end tell
    end tell
    '''
    success, out, _ = run_osascript(script)
    try:
        return int(out.strip())
    except:
        return 0

def click_section(section_idx: int) -> bool:
    script = f'''
    tell application "System Events"
        tell process "Microsoft OneNote"
            try
                click button {section_idx} of toolbar 1 of window 1
                return true
            on error
                return false
            end try
        end tell
    end tell
    '''
    success, out, _ = run_osascript(script)
    return success and out.strip() == "true"

def click_page(page_idx: int) -> bool:
    script = f'''
    tell application "System Events"
        tell process "Microsoft OneNote"
            try
                click row {page_idx} of outline 1 of scroll area 1 of splitter group 1 of window 1
                return true
            on error
                return false
            end try
        end tell
    end tell
    '''
    success, out, _ = run_osascript(script)
    return success and out.strip() == "true"

def get_page_name(page_idx: int) -> str:
    script = f'''
    tell application "System Events"
        tell process "Microsoft OneNote"
            try
                return value of static text 1 of scroll area 1 of splitter group 1 of window 1
            on error
                try
                    return name of row {page_idx} of outline 1 of scroll area 1 of splitter group 1 of window 1
                on error
                    return "Page_" & {page_idx}
                end try
            end try
        end tell
    end tell
    '''
    success, out, _ = run_osascript(script)
    return out.strip() if success and out.strip() else f"Page_{page_idx}"

def get_section_name(section_idx: int) -> str:
    script = f'''
    tell application "System Events"
        tell process "Microsoft OneNote"
            try
                return value of button {section_idx} of toolbar 1 of window 1
            on error
                return "Section_" & {section_idx}
            end try
        end tell
    end tell
    '''
    success, out, _ = run_osascript(script)
    return out.strip() if success and out.strip() else f"Section_{section_idx}"

def export_current_page_as_pdf(export_folder: Path, file_name: str) -> bool:
    """Export current page as PDF using File > Export > PDF."""
    file_path = EXPORT_FOLDER / file_name
    
    # File > Export...
    script = '''
    tell application "System Events"
        tell process "Microsoft OneNote"
            click menu item "Export..." of menu "File" of menu bar 1
        end tell
    end tell
    '''
    success, _, err = run_osascript(script)
    if not success:
        logger.warning(f"Failed to open Export dialog: {err}")
        return False
    time.sleep(2)
    
    # Select PDF format
    script = '''
    tell application "System Events"
        tell process "Microsoft OneNote"
            click pop up button 1 of window "Export"
        end tell
    end tell
    '''
    run_osascript(script)
    time.sleep(0.5)
    
    script = '''
    tell application "System Events"
        tell process "Microsoft OneNote"
            click menu item "PDF" of menu 1 of pop up button 1 of window "Export"
        end tell
    end tell
    '''
    run_osascript(script)
    time.sleep(0.5)
    
    # Click Export button
    script = '''
    tell application "System Events"
        tell process "Microsoft OneNote"
            click button "Export" of window "Export"
        end tell
    end tell
    '''
    run_osascript(script)
    time.sleep(1)
    
    # Save dialog - Go to folder
    script = '''
    tell application "System Events"
        keystroke "g" using {command down, shift down}
    end tell
    '''
    run_osascript(script)
    time.sleep(0.5)
    
    # Type export folder path
    run_osascript(f'tell application "System Events" to keystroke "{str(EXPORT_FOLDER)}"')
    time.sleep(0.5)
    run_osascript('tell application "System Events" to keystroke return')
    time.sleep(0.5)
    
    # Type file name
    run_osascript(f'tell application "System Events" to keystroke "{str(file_name)}"')
    time.sleep(0.5)
    run_osascript('tell application "System Events" to keystroke return')
    time.sleep(3)
    
    # Verify file exists
    return file_path.exists()

def main():
    logger.info("=== OneNote Export Started ===")
    logger.info(f"Notebook: {NOTEBOOK_NAME}")
    logger.info(f"Export folder: {EXPORT_FOLDER}")
    
    EXPORT_FOLDER.mkdir(parents=True, exist_ok=True)
    
    start_section, start_page = load_checkpoint()
    logger.info(f"Resuming from section {start_section}, page {start_page}")
    
    # Activate OneNote
    run_osascript('tell application "Microsoft OneNote" to activate')
    time.sleep(2)
    
    # Get section count
    section_count = get_section_count()
    logger.info(f"Found {section_count} sections")
    
    for s in range(start_section + 1, section_count + 1):
        if s == 1 and start_section == 0:
            s = 1
        
        logger.info(f"Processing section {s}/{section_count}")
        
        if not click_section(s):
            logger.warning(f"Could not click section {s}")
            continue
        time.sleep(1)
        
        page_count = get_page_count()
        logger.info(f"Section {s}: {page_count} pages")
        
        if page_count == 0:
            continue
        
        p_start = 1
        if s == start_section + 1:
            p_start = start_page + 1
            if p_start < 1:
                p_start = 1
        
        for p in range(p_start, page_count + 1):
            logger.info(f"  Page {p}/{page_count}")
            
            if not click_page(p):
                logger.warning(f"Could not click page {p}")
                continue
            time.sleep(1)
            
            page_name = get_page_name(p)
            section_name = get_section_name(s)
            
            safe_page = sanitize_name(page_name)
            safe_section = sanitize_name(section_name)
            file_name = f"{safe_section}_{safe_page}.pdf"
            file_path = EXPORT_FOLDER / file_name
            
            logger.info(f"Exporting: {file_name}")
            
            success = export_current_page_as_pdf(EXPORT_FOLDER, file_name)
            
            if success and (EXPORT_FOLDER / file_name).exists():
                logger.info(f"Exported: {file_name}")
            else:
                logger.warning(f"Failed: {file_name}")
            
            save_checkpoint(s, p + 1, page_name)
            time.sleep(2)
    
    logger.info("=== OneNote Export Complete ===")
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
    
    run_osascript(f'display notification "OneNote export complete!" with title "MFL Brain" subtitle "Exported to {EXPORT_FOLDER}"')

if __name__ == "__main__":
    main()