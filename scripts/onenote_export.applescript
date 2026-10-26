#!/usr/bin/env osascript
-- OneNote Mac UI Automation: Export MFL Staff Notebook pages as PDFs
-- Integer delays only

use AppleScript version "2.4"
use scripting additions

property notebookName : "MFL Staff Notebook"
property exportFolder : "/Volumes/GCH/Second Brain GCH Work /OneNote Exports/PDFs"
property checkpointFile : "/Volumes/GCH/Second Brain GCH Work /OneNote Exports/.export_checkpoint.json"

on logMessage(msg)
    set timestamp to do shell script "date '+%Y-%m-%d %H:%M:%S'"
    set logEntry to "[" & timestamp & "] " & msg
    log logEntry
    do shell script "echo " & quoted form of logEntry & " >> " & quoted form of (exportFolder & "/../export.log")
end logMessage

on saveCheckpoint(sectionIndex, pageIndex, pageName)
    set checkpointData to "{ \"sectionIndex\": " & sectionIndex & ", \"pageIndex\": " & pageIndex & ", \"pageName\": \"" & pageName & "\", \"timestamp\": \"" & (do shell script "date -u +'%Y-%m-%dT%H:%M:%SZ'") & "\" }"
    do shell script "echo " & quoted form of checkpointData & " > " & quoted form of checkpointFile
end saveCheckpoint

on loadCheckpoint()
    try
        set checkpointText to do shell script "cat " & quoted form of checkpointFile
        set sectionIndex to (do shell script "echo " & quoted form of checkpointText & " | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"sectionIndex\"])'" 2>/dev/null) as integer
        set pageIndex to (do shell script "echo " & quoted form of checkpointText & " | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"pageIndex\"])'" 2>/dev/null) as integer
        return {sectionIndex:sectionIndex, pageIndex:pageIndex}
    on error
        return {sectionIndex:0, pageIndex:0}
    end try
end loadCheckpoint

on sanitizeName(name)
    set cleanName to name
    set cleanName to my replaceText(cleanName, "/", "-")
    set cleanName to my replaceText(cleanName, ":", "-")
    set cleanName to my replaceText(cleanName, "*", "-")
    set cleanName to my replaceText(cleanName, "?", "-")
    set cleanName to my replaceText(cleanName, "\"", "-")
    set cleanName to my replaceText(cleanName, "<", "-")
    set cleanName to my replaceText(cleanName, ">", "-")
    set cleanName to my replaceText(cleanName, "|", "-")
    set cleanName to my replaceText(cleanName, "\\", "-")
    return cleanName
end sanitizeName

on replaceText(source, find, replace)
    set prevTID to AppleScript's text item delimiters
    set AppleScript's text item delimiters to find
    set items to text items of source
    set AppleScript's text item delimiters to replace
    set result to items as text
    set AppleScript's text item delimiters to prevTID
    return result
end replaceText

on run
    logMessage("=== OneNote Export Started ===")
    logMessage("Notebook: " & notebookName)
    logMessage("Export folder: " & exportFolder)
    
    do shell script "mkdir -p " & quoted form of exportFolder
    
    set cp to loadCheckpoint()
    set startSection to sectionIndex of cp
    set startPage to pageIndex of cp
    logMessage("Resuming from section " & startSection & ", page " & startPage)
    
    tell application "Microsoft OneNote"
        activate
        delay 2
        
        tell application "System Events"
            tell process "Microsoft OneNote"
                repeat until exists window 1
                    delay 1
                end repeat
                set frontmost to true
                delay 1
                
                -- Navigate to notebook
                try
                    click menu item notebookName of menu "Notebooks" of menu bar item "Notebooks" of menu bar 1
                    delay 1
                on error
                    logMessage("Could not find notebook in menu")
                end try
                
                -- Get section count
                set sectionCount to 0
                try
                    set sectionButtons to buttons of toolbar 1 of window 1
                    set sectionCount to count of sectionButtons
                    logMessage("Found " & sectionCount & " sections")
                on error
                    logMessage("Could not count sections")
                end try
                
                repeat with s from (startSection + 1) to sectionCount
                    if s = 1 and startSection = 0 then set s to 1
                    
                    try
                        click button s of toolbar 1 of window 1
                        delay 1
                    on error
                        logMessage("Could not click section " & s)
                        continue
                    end try
                    
                    set pageCount to 0
                    try
                        set pageRows to rows of outline 1 of scroll area 1 of splitter group 1 of window 1
                        set pageCount to count of pageRows
                        logMessage("Section " & s & ": " & pageCount & " pages")
                    on error
                        logMessage("Could not count pages in section " & s)
                        continue
                    end try
                    
                    set pStart to 1
                    if s = (startSection + 1) then
                        set pStart to startPage + 1
                        if pStart < 1 then set pStart to 1
                    end if
                    
                    repeat with p from pStart to pageCount
                        try
                            click row p of outline 1 of scroll area 1 of splitter group 1 of window 1
                            delay 1
                        on error
                            logMessage("Could not click page " & p & " in section " & s)
                            continue
                        end try
                        
                        set pageName to "Page_" & p
                        try
                            set pageName to value of static text 1 of scroll area 1 of splitter group 1 of window 1
                        on error
                            try
                                set pageName to name of row p of outline 1 of scroll area 1 of splitter group 1 of window 1
                            end try
                        end try
                        
                        set safePageName to sanitizeName(pageName)
                        set sectionName to "Section_" & s
                        try
                            set sectionName to sanitizeName(value of button s of toolbar 1 of window 1)
                        end try
                        
                        set fileName to sectionName & "_" & safePageName & ".pdf"
                        set filePath to exportFolder & "/" & fileName
                        
                        logMessage("Exporting: " & fileName)
                        
                        try
                            click menu item "Export..." of menu "File" of menu bar 1
                            delay 2
                            
                            click pop up button 1 of window "Export"
                            delay 1
                            click menu item "PDF" of menu 1 of pop up button 1 of window "Export"
                            delay 1
                            
                            click button "Export" of window "Export"
                            delay 1
                            
                            keystroke "g" using {command down, shift down}
                            delay 1
                            keystroke exportFolder
                            delay 1
                            keystroke return
                            delay 1
                            
                            keystroke fileName
                            delay 1
                            keystroke return
                            delay 3
                            
                            set fileExists to do shell script "test -f " & quoted form of filePath & " && echo YES || echo NO"
                            if fileExists = "YES" then
                                logMessage("Exported: " & fileName)
                            else
                                logMessage("Failed: " & fileName)
                            end if
                            
                        on error errMsg
                            logMessage("Export error for " & fileName & ": " & errMsg)
                        end try
                        
                        saveCheckpoint(s, p + 1, pageName)
                        delay 2
                    end repeat
                end repeat
            end tell
        end tell
    end tell
    
    logMessage("=== OneNote Export Complete ===")
    do shell script "rm -f " & quoted form of checkpointFile
    
    display notification "OneNote export complete!" with title "MFL Brain" subtitle "Exported to " & exportFolder
end run