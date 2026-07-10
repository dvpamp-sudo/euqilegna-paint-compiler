from pathlib import Path
import json, cv2, numpy as np
from PIL import Image
from scipy import ndimage as ndi
from skimage import color, measure, segmentation
from shapely.geometry import Polygon
from shapely.ops import unary_union

def rgb_hex(c): return '#%02X%02X%02X' % tuple(int(v) for v in c)
def cfg(detail): return {'simple':(650,700),'balanced':(850,1100),'detailed':(1050,1800)}.get(detail,(850,1100))
def resize(img,max_side):
    h,w=img.shape[:2]; s=min(max_side/max(h,w),1.0)
    return cv2.resize(img,(round(w*s),round(h*s)),interpolation=cv2.INTER_AREA) if s<1 else img

def means(rgb,labels): return {int(i): rgb[labels==i].mean(axis=0) for i in np.unique(labels)}
def adjacency(labels):
    out={int(i):set() for i in np.unique(labels)}
    for a,b in ((labels[:,:-1],labels[:,1:]),(labels[:-1,:],labels[1:,:])):
        m=a!=b
        for x,y in zip(a[m],b[m]): out[int(x)].add(int(y)); out[int(y)].add(int(x))
    return out

def merge_regions(rgb,labels,strength,min_area):
    labels=labels.copy()
    for _ in range(8):
        changed=False; mu=means(rgb,labels); adj=adjacency(labels)
        ids,counts=np.unique(labels,return_counts=True); areas=dict(zip(map(int,ids),map(int,counts)))
        for rid in sorted(areas,key=areas.get):
            ns=list(adj.get(rid,[]))
            if not ns: continue
            dist,best=min((float(np.linalg.norm(mu[rid]-mu[n])),n) for n in ns)
            if areas[rid]<min_area or dist<=strength:
                labels[labels==rid]=best; changed=True
        labels=measure.label(labels,connectivity=1,background=-1)
        if not changed: break
    return labels.astype(np.int32)

def palette(rgb,regions,k):
    mu=means(rgb,regions); ids=sorted(mu); data=np.array([mu[i] for i in ids],np.float32); k=min(k,len(ids))
    crit=(cv2.TERM_CRITERIA_EPS+cv2.TERM_CRITERIA_MAX_ITER,50,0.3)
    _,labs,centers=cv2.kmeans(data,k,None,crit,5,cv2.KMEANS_PP_CENTERS)
    centers=np.clip(centers,0,255).astype(np.uint8); mp={rid:int(labs[i][0]) for i,rid in enumerate(ids)}
    cmap=np.vectorize(mp.get)(regions).astype(np.int32)
    return cmap,centers

def polygon(mask):
    polys=[]
    for c in measure.find_contours(mask.astype(np.uint8),0.5):
        if len(c)<8: continue
        p=Polygon([(float(x),float(y)) for y,x in c])
        if not p.is_valid: p=p.buffer(0)
        if not p.is_empty and p.area>4: polys.append(p)
    if not polys: return None
    m=unary_union(polys)
    if m.geom_type=='MultiPolygon': m=max(m.geoms,key=lambda p:p.area)
    return m.simplify(1.4,preserve_topology=True)

def path(poly):
    pts=list(poly.exterior.coords)
    if len(pts)<4:return ''
    d=[f'M {pts[0][0]:.2f} {pts[0][1]:.2f}']+[f'L {x:.2f} {y:.2f}' for x,y in pts[1:]]+['Z']
    for ring in poly.interiors:
        q=list(ring.coords); d += [f'M {q[0][0]:.2f} {q[0][1]:.2f}']+[f'L {x:.2f} {y:.2f}' for x,y in q[1:]]+['Z']
    return ' '.join(d)

def label_point(mask):
    d=ndi.distance_transform_edt(mask); y,x=np.unravel_index(np.argmax(d),d.shape); return float(x),float(y)

def compile_artwork(input_path,output_dir,colors=24,detail='balanced',min_region_area=220,merge_strength=28,outline_width=0.5):
    input_path=Path(input_path); output_dir=Path(output_dir); output_dir.mkdir(parents=True,exist_ok=True)
    rgb=np.array(Image.open(input_path).convert('RGB')); max_side,nseg=cfg(detail); rgb=resize(rgb,max_side)
    rgb=cv2.bilateralFilter(rgb,9,70,70)
    slic=segmentation.slic(color.rgb2lab(rgb),n_segments=nseg,compactness=10,sigma=1,start_label=0,channel_axis=-1)
    merged=merge_regions(rgb,slic,merge_strength,min_region_area)
    cmap,centers=palette(rgb,merged,colors)
    regs=[]; n=1
    for rid in np.unique(merged):
        mask=merged==rid; area=int(mask.sum())
        if area<min_region_area: continue
        poly=polygon(mask)
        if poly is None: continue
        d=path(poly)
        if not d: continue
        cid=int(np.bincount(cmap[mask]).argmax()); x,y=label_point(mask)
        regs.append({'regionId':f'region_{n}','colorId':str(cid+1),'fillColor':rgb_hex(centers[cid]),'painted':False,'area':area,'path':d,'label':{'x':x,'y':y}}); n+=1
    regs.sort(key=lambda r:r['area'],reverse=True)
    if not regs: raise ValueError('No regions generated. Lower minimum region area.')
    h,w=rgb.shape[:2]; Image.fromarray(rgb).save(output_dir/'preview.png')
    pal=[{'colorId':str(i+1),'hex':rgb_hex(c),'rgb':[int(v) for v in c],'label':f'Color {i+1}'} for i,c in enumerate(centers)]
    style=f'.paint-region{{stroke:#111;stroke-width:{outline_width};stroke-linecap:round;stroke-linejoin:round;vector-effect:non-scaling-stroke;cursor:pointer}}.region-number{{font-family:Arial;font-size:11px;font-weight:700;text-anchor:middle;dominant-baseline:middle;pointer-events:none}}'
    col=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}"><style>{style}</style>']
    blank=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}"><defs><pattern id="checker" width="12" height="12" patternUnits="userSpaceOnUse"><rect width="6" height="6" fill="#c8c8c8"/><rect x="6" y="6" width="6" height="6" fill="#c8c8c8"/><rect x="6" width="6" height="6" fill="#eee"/><rect y="6" width="6" height="6" fill="#eee"/></pattern></defs><style>{style}.paint-region{{fill:#fff}}.paint-region.active-target{{fill:url(#checker)}}</style>']
    for r in regs:
        attrs=f'class="paint-region" id="{r["regionId"]}" data-region-id="{r["regionId"]}" data-color-id="{r["colorId"]}" data-fill-color="{r["fillColor"]}" data-painted="false" fill-rule="evenodd" d="{r["path"]}"'
        col.append(f'<path {attrs} fill="{r["fillColor"]}"/>'); blank.append(f'<path {attrs}/>')
    for r in regs:
        if r['area']>=450:
            x,y=r['label']['x'],r['label']['y']; col.append(f'<text class="region-number" x="{x:.1f}" y="{y:.1f}">{r["colorId"]}</text>'); blank.append(f'<text class="region-number" data-for-region="{r["regionId"]}" x="{x:.1f}" y="{y:.1f}">{r["colorId"]}</text>')
    col.append('</svg>'); blank.append('</svg>')
    (output_dir/'paintMap_colored.svg').write_text('\n'.join(col)); (output_dir/'paintMap.svg').write_text('\n'.join(blank))
    (output_dir/'palette.json').write_text(json.dumps(pal,indent=2)); (output_dir/'regions.json').write_text(json.dumps(regs,indent=2))
    meta={'engine':'Euqilegna Paint Compiler','version':'1.0 Phase 1','inputFile':input_path.name,'width':w,'height':h,'regions':len(regs),'colors':len(pal)}
    (output_dir/'metadata.json').write_text(json.dumps(meta,indent=2)); return meta
