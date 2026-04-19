# Universal GUI Desktop Access Skill

This downloadable skill is intended to let Clai operate nearly any GUI-based application on the local computer.
Use it only on systems you own or have explicit permission to automate.

## Scope

- Launch and focus desktop apps.
- Inspect visible UI with screenshots and OCR.
- Click, type, and navigate through windows, menus, and dialogs.
- Run multi-step GUI workflows with checkpoints.

## Safety Requirements

- Ask for user confirmation before destructive actions.
- Keep automation scoped to the named app and window.
- Announce planned input actions before executing them.
- Stop and ask for guidance if UI state is uncertain.
- Never perform hidden background actions outside the requested task.

## Suggested Dependencies

Install these in the runtime where automation will execute:

- pyautogui
- pygetwindow
- pillow
- mss
- pytesseract

## Suggested Operating Pattern

1. Confirm target application and boundaries.
2. Bring target window to front.
3. Capture screenshot and verify target controls.
4. Execute one action at a time with validation after each action.
5. Report results with screenshots or logs.

## Example Prompt Template

"Control the [application name] window only. Before each click or typed input, tell me the exact action. Wait for my confirmation on irreversible actions such as delete, send, submit, purchase, transfer, or publish."
