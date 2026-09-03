import json
import random


def walk(seed, n, drift, vol, start=1.0):
    r = random.Random(seed); v = start; out=[]
    for i in range(n):
        v *= 1 + r.gauss(drift, vol); out.append(v)
    return out
def path(vals, w, h, pad=0):
    lo, hi = min(vals), max(vals); rng = hi-lo or 1
    pts = [(pad + i*(w-2*pad)/(len(vals)-1), pad + (h-2*pad)*(1-(v-lo)/rng)) for i,v in enumerate(vals)]
    return " ".join(f"{x:.1f},{y:.1f}" for x,y in pts), pts
def dd(vals):
    peak=vals[0]; out=[]
    for v in vals:
        peak=max(peak,v); out.append((v-peak)/peak)
    return out
res={}
main = walk(7, 120, 0.0032, 0.02)
res['main_equity'], pts = path(main, 640, 220)
d = dd(main)
res['main_dd'], dpts = path(d, 640, 120)
res['main_dd_area'] = res['main_dd'] + f" 640.0,{dpts[0][1]:.1f}" 
# for area fill close along top (0 dd = top). find y of 0:
lo,hi=min(d),max(d)
y0 = 120*(1-(0-lo)/(hi-lo))
res['main_dd_y0']=round(y0,1)
res['main_stats']=dict(total=(main[-1]/main[0]-1)*100, maxdd=min(d)*100)
for name,seed,dr,vol in [("s1",11,0.003,0.018),("s2",3,0.0015,0.012),("s3",5,0.004,0.035),("s4",9,-0.0005,0.02),("s5",13,0.0022,0.015),("s6",21,0.0028,0.022)]:
    v=walk(seed,60,dr,vol); res[name+'_spark'],_=path(v,120,32,2); res[name+'_ret']=round((v[-1]/v[0]-1)*100,1); res[name+'_dd']=round(min(dd(v))*100,1)
print(json.dumps(res,indent=1))
