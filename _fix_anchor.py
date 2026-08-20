with open(r"D:\work\ac-advisor\ac_advisor.py", "r", encoding="utf-8") as f:
    src = f.read()

src = src.replace("if 0 <= mins < 30:", "if 0 <= mins < 15:")

with open(r"D:\work\ac-advisor\ac_advisor.py", "w", encoding="utf-8") as f:
    f.write(src)
print("Fixed: manual anchor cooldown 30->15 min")
