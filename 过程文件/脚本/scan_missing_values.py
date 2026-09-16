import json,glob,os
rows=[]
for p in sorted(glob.glob(r"D:\workspace\DrawClimateChart\过程文件\侦察\_probe_tmp\*.json")):
    try: d=json.load(open(p,encoding="utf-8-sig"))["city"]
    except Exception as e: continue
    cl=d.get("climate") or {}
    ms=cl.get("climateMonth") or []
    if not ms: continue
    def miss(k): return sum(1 for m in ms if m.get(k) in (None,"","NULL"))
    rf,rd,mn,mx=miss('rainfall'),miss('raindays'),miss('minTemp'),miss('maxTemp')
    if rf or rd or mn or mx:
        rows.append(f"{os.path.basename(p):<12} {d.get('cityName','?'):<10} raintype={cl.get('raintype')!r:<10} rainfall缺失={rf} raindays缺失={rd} min缺失={mn} max缺失={mx}")
print("有缺失值的城市：")
print("\n".join(rows) if rows else "（无）")
print()
print("全部抽样城市数：", len(glob.glob(r"D:\workspace\DrawClimateChart\过程文件\侦察\_probe_tmp\*.json")))
