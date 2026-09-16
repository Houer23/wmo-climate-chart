import json,urllib.request,urllib.error
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
H={"User-Agent":UA,"Accept":"application/json,text/xml,*/*;q=0.8","Accept-Language":"zh-CN,zh;q=0.9"}
lines=[]
for cid in (123456, 237, 1, 156):
    for attempt in (1,2):
        try:
            r=urllib.request.urlopen(urllib.request.Request(f"https://worldweather.wmo.int/zh/json/{cid}_zh.xml",headers=H),timeout=45)
            b=r.read()
            d=json.loads(b.decode("utf-8-sig"))
            c=d.get("city",d)
            cl=c.get("climate") or {}
            ms=cl.get("climateMonth") or []
            lines.append(f"cityId={cid} try{attempt} HTTP{r.status} len={len(b)} name={c.get('cityName')} mem={ (c.get('member') or {}).get('memName') } raintype={cl.get('raintype')!r} unit={cl.get('rainunit')!r} months={len(ms)} datab={cl.get('datab')} datae={cl.get('datae')}")
            lines.append("   m1="+json.dumps(ms[0],ensure_ascii=False) if ms else "   no months")
            break
        except urllib.error.HTTPError as e:
            lines.append(f"cityId={cid} try{attempt} HTTPError {e.code}")
            break
        except Exception as e:
            lines.append(f"cityId={cid} try{attempt} {type(e).__name__}: {e}")
open(r"C:\Users\user\WorkBuddy\2026-09-16-16-36-56\过程文件\侦察\_recon_probe3_report.txt","w",encoding="utf-8").write("\n".join(lines))
