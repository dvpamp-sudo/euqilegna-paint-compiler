from __future__ import annotations


def build_pointer_engine_script() -> str:
    """
    Return the Version 9 pointer/touch interaction engine.

    It resolves the painted region from the actual pointer coordinate in SVG
    space instead of depending on fragile transparent overlay click bindings.
    """
    return r"""
(function installEuqilegnaPointerEngineV9(){
  const ENGINE_VERSION='9.0.0';
  const artboard=document.getElementById('artboard');
  const host=document.getElementById('svgHost');

  if(!artboard || !host || !Array.isArray(REGIONS)){
    console.warn('Euqilegna Pointer Engine V9 could not initialize.');
    return;
  }

  let pointerDown=null;
  let lastHandledAt=0;
  let lastHandledRegion=null;

  const asText=value=>String(value ?? '');

  function getSvg(){
    return host.querySelector('svg');
  }

  function toSvgPoint(clientX,clientY){
    const svg=getSvg();
    if(!svg) return null;
    const matrix=svg.getScreenCTM();
    if(!matrix) return null;

    const point=svg.createSVGPoint();
    point.x=clientX;
    point.y=clientY;
    return point.matrixTransform(matrix.inverse());
  }

  function candidateRegions(){
    return REGIONS.filter(region=>{
      if(asText(region.colorId)!==asText(selectedColor)) return false;
      if(painted[region.regionId]) return false;
      return Boolean(document.getElementById(region.regionId));
    });
  }

  function pathContainsPoint(path,point){
    if(!path || !point) return false;

    try{
      if(typeof path.isPointInFill==='function' && path.isPointInFill(point)){
        return true;
      }
    }catch(_error){}

    // Fallback for browsers with incomplete SVGGeometryElement support.
    try{
      const box=path.getBBox();
      return (
        point.x>=box.x &&
        point.x<=box.x+box.width &&
        point.y>=box.y &&
        point.y<=box.y+box.height
      );
    }catch(_error){
      return false;
    }
  }

  function distanceToLabel(region,point){
    const x=Number(region.label?.x ?? 0);
    const y=Number(region.label?.y ?? 0);
    return Math.hypot(point.x-x,point.y-y);
  }

  function areaOf(region){
    const area=Number(region.area);
    return Number.isFinite(area) && area>0 ? area : Number.MAX_SAFE_INTEGER;
  }

  function resolveRegionAt(clientX,clientY){
    const point=toSvgPoint(clientX,clientY);
    if(!point) return null;

    const direct=[];
    for(const region of candidateRegions()){
      const path=document.getElementById(region.regionId);
      if(pathContainsPoint(path,point)){
        direct.push({region,path});
      }
    }

    if(direct.length){
      // Prefer the smallest containing region so tiny details are not swallowed
      // by a larger background region drawn beneath them.
      direct.sort((a,b)=>
        areaOf(a.region)-areaOf(b.region) ||
        distanceToLabel(a.region,point)-distanceToLabel(b.region,point)
      );
      return direct[0];
    }

    // Accessibility fallback: allow a small nearest-label tolerance for very
    // thin regions and Apple Pencil taps that land a few pixels outside.
    const svg=getSvg();
    const viewBox=svg?.viewBox?.baseVal;
    const rect=svg?.getBoundingClientRect();
    const unitsPerPixel=(
      viewBox && rect && rect.width
        ? viewBox.width/rect.width
        : 1
    );
    const tolerance=Math.max(5*unitsPerPixel,2.5);

    const nearby=candidateRegions()
      .map(region=>({
        region,
        path:document.getElementById(region.regionId),
        distance:distanceToLabel(region,point),
      }))
      .filter(item=>item.distance<=tolerance)
      .sort((a,b)=>a.distance-b.distance || areaOf(a.region)-areaOf(b.region));

    return nearby[0] || null;
  }

  function paintResolved(resolved,event){
    if(!resolved) return false;
    const {region,path}=resolved;

    if(asText(region.colorId)!==asText(selectedColor)){
      return false;
    }

    event?.preventDefault?.();
    event?.stopPropagation?.();
    event?.stopImmediatePropagation?.();

    lastHandledAt=performance.now();
    lastHandledRegion=region.regionId;
    handleRegion(region,path);
    return true;
  }

  function movementDistance(start,event){
    if(!start) return Number.POSITIVE_INFINITY;
    return Math.hypot(
      Number(event.clientX)-start.x,
      Number(event.clientY)-start.y
    );
  }

  artboard.addEventListener('pointerdown',event=>{
    if(event.button!==undefined && event.button!==0) return;
    pointerDown={
      id:event.pointerId,
      x:Number(event.clientX),
      y:Number(event.clientY),
      time:performance.now(),
    };
  },true);

  artboard.addEventListener('pointercancel',()=>{
    pointerDown=null;
  },true);

  artboard.addEventListener('pointerup',event=>{
    if(!pointerDown || pointerDown.id!==event.pointerId) return;

    const moved=movementDistance(pointerDown,event);
    const elapsed=performance.now()-pointerDown.time;
    pointerDown=null;

    // Preserve pan/drag and long-press behavior.
    if(hasDragged || moved>10 || elapsed>1200) return;

    const resolved=resolveRegionAt(event.clientX,event.clientY);
    paintResolved(resolved,event);
  },true);

  // Suppress the synthetic click emitted after a pointerup we already handled.
  artboard.addEventListener('click',event=>{
    if(performance.now()-lastHandledAt<700){
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
    }
  },true);

  // Keyboard-accessible region painting for focused SVG paths.
  artboard.addEventListener('keydown',event=>{
    if(event.key!=='Enter' && event.key!==' ') return;
    const target=event.target?.closest?.('.paint-region,.region-hit');
    if(!target) return;

    const regionId=
      target.getAttribute('data-target-region') ||
      target.getAttribute('data-region-id') ||
      target.id;
    const region=REGIONS.find(item=>item.regionId===regionId);
    const path=region ? document.getElementById(region.regionId) : null;
    if(region && path){
      paintResolved({region,path},event);
    }
  },true);

  // Make all visible paths keyboard focusable and expose their number.
  for(const region of REGIONS){
    const path=document.getElementById(region.regionId);
    if(path){
      path.setAttribute('tabindex','0');
      path.setAttribute('role','button');
      path.setAttribute(
        'aria-label',
        `Paint color ${region.colorId} section`
      );
      path.dataset.pointerEngine='v9';
    }
  }

  window.EuqilegnaPointerEngineV9={
    version:ENGINE_VERSION,
    resolveRegionAt,
    selectedColor:()=>selectedColor,
    lastHandledRegion:()=>lastHandledRegion,
  };

  console.info(`Euqilegna Pointer Engine ${ENGINE_VERSION} ready.`);
})();
"""


__all__ = ["build_pointer_engine_script"]
