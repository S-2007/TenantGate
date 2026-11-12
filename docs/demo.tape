# VHS tape for a short demo GIF: https://github.com/charmbracelet/vhs
#   1. start the API in another terminal:  python -m uvicorn app.main:app --port 8000
#   2. vhs docs/demo.tape                 -> writes docs/demo.gif
# Alternative with asciinema:  asciinema rec -c "bash scripts/demo.sh" docs/demo.cast
Output docs/demo.gif
Set Shell "bash"
Set FontSize 15
Set Width 1300
Set Height 820
Set TypingSpeed 40ms
Type "bash scripts/demo.sh"
Enter
Sleep 45s
