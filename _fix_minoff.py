with open(r"D:\work\ac-advisor\ac_advisor.py", "r", encoding="utf-8") as f:
    src = f.read()

src = src.replace("DAY_MIN_OFF = 15", "DAY_MIN_OFF = 10")
src = src.replace("MIN_OFF = 30", "MIN_OFF = 15")

with open(r"D:\work\ac-advisor\ac_advisor.py", "w", encoding="utf-8") as f:
    f.write(src)
print("MIN_OFF: 30->15, DAY_MIN_OFF: 15->10")
