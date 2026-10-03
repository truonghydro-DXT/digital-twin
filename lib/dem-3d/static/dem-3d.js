/**
 * DEM / GeoTIFF 3D viewer (Three.js r128 + OrbitControls).
 * Có bản đồ nền OSM dưới mesh (tile XYZ theo viewport khi pan/zoom).
 */
(function (global) {
  let renderer = null;
  let scene = null;
  let camera = null;
  let controls = null;
  let animationId = null;
  let currentMesh = null;
  let previewMesh = null;
  let osmMesh = null;
  let contourGroup = null;
  let resizeHandler = null;
  let exaggeration = 0.05;
  let contoursVisible = false;
  let lastTerrain = null;
  let lastPoints = null;
  let lastPointAppearance = { size: 4, colorMode: 'rgb', color: '#38bdf8' };
  let osmRefreshTimer = null;
  let pointExagTimer = null;
  let lodIdleTimer = null;
  let pointInteracting = false;
  let pointLodMode = '';
  let pointFitDist = 200;
  let renderDirty = true;
  const POINT_INTERACT_MAX = 60000;
  const POINT_CHUNK_AXIS = 32;
  let osmBuildId = 0;
  let lastOsmKey = '';
  let regionDraw = {
    active: false,
    closed: false,
    points: [],
    group: null,
    downX: 0,
    downY: 0
  };
  let regionDrawOnDown = null;
  let regionDrawOnUp = null;
  let regionDrawOnDbl = null;
  let regionDrawOnKey = null;
  /** Ứng viên nhãn contour + sprite đang hiện theo viewport */
  let contourLabelData = [];
  let contourLabelSprites = [];
  let contourLabelLayer = null;
  let labelsNeedUpdate = true;
  const _labelFrustum = new THREE.Frustum();
  const _labelProj = new THREE.Matrix4();
  const _labelWorld = new THREE.Vector3();
  const _labelNdc = new THREE.Vector3();
  const CONTOUR_LABEL_MAX_VISIBLE = 28;
  const CONTOUR_LABEL_MIN_NDC_DIST = 0.11;

  /** Cạnh DEM trong không gian Three.js */
  const DEM_WORLD_W = 200;
  /** Zoom camera tối đa (tương đương mức 32 bản đồ) */
  const DEM3D_MAX_ZOOM = 32;
  /** Mức zoom quy ước khi vừa fit DEM vào khung */
  const DEM3D_FIT_ZOOM = 14;
  /** OSM tile gốc chỉ tới 19; z cao hơn dùng overzoom */
  const OSM_NATIVE_MAX_ZOOM = 19;
  /** Vành đai tile OSM quanh viewport */
  const OSM_MARGIN_TILES = 2;
  /** Giới hạn số tile OSM trong scene */
  const OSM_MAX_TILES = 180;

  function $(id) {
    return document.getElementById(id);
  }

  function getOpenEntries() {
    if (typeof global.getOpenGeotiffEntries === 'function') {
      return global.getOpenGeotiffEntries();
    }
    return [];
  }

  function pickFileIdFor3D() {
    const entries = getOpenEntries();
    if (entries.length) return entries[entries.length - 1].fileId;
    return null;
  }

  function disposeObject3D(root) {
    if (!root) return;
    if (scene) scene.remove(root);
    root.traverse(function (obj) {
      if (obj.geometry) obj.geometry.dispose();
      if (obj.material) {
        const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
        mats.forEach(function (m) {
          if (m.map) m.map.dispose();
          m.dispose();
        });
      }
    });
  }

  function stopLoop() {
    if (animationId != null) {
      cancelAnimationFrame(animationId);
      animationId = null;
    }
  }

  function destroyViewer() {
    clearRegionDraw(true);
    stopLoop();
    if (resizeHandler) {
      window.removeEventListener('resize', resizeHandler);
      resizeHandler = null;
    }
    disposeObject3D(previewMesh);
    previewMesh = null;
    pointInteracting = false;
    pointLodMode = '';
    renderDirty = true;
    if (lodIdleTimer) {
      clearTimeout(lodIdleTimer);
      lodIdleTimer = null;
    }
    disposeObject3D(currentMesh);
    currentMesh = null;
    disposeObject3D(osmMesh);
    osmMesh = null;
    disposeObject3D(contourGroup);
    contourGroup = null;
    clearContourLabels();
    contoursVisible = false;
    lastTerrain = null;
    lastPoints = null;
    if (pointExagTimer) {
      cancelAnimationFrame(pointExagTimer);
      pointExagTimer = null;
    }
    lastOsmKey = '';
    osmBuildId += 1;
    if (osmRefreshTimer) {
      clearTimeout(osmRefreshTimer);
      osmRefreshTimer = null;
    }
    if (controls) {
      controls.dispose();
      controls = null;
    }
    if (renderer) {
      try {
        const gl = renderer.getContext();
        const lose = gl && gl.getExtension && gl.getExtension('WEBGL_lose_context');
        if (lose) lose.loseContext();
      } catch (e) {}
      renderer.dispose();
      const el = $('view3d');
      if (el && renderer.domElement.parentNode === el) {
        el.removeChild(renderer.domElement);
      }
      renderer = null;
    }
    scene = null;
    camera = null;
  }

  function initViewer(opts) {
    opts = opts || {};
    const performance = !!opts.performance;
    const container = $('view3d');
    if (!container || !global.THREE) {
      throw new Error('Thiếu container #view3d hoặc thư viện Three.js');
    }
    if (!THREE.OrbitControls) {
      throw new Error('Thiếu THREE.OrbitControls');
    }

    destroyViewer();

    const w = container.clientWidth || window.innerWidth;
    const h = container.clientHeight || window.innerHeight;

    scene = new THREE.Scene();
    scene.background = new THREE.Color(opts.background != null ? opts.background : 0x87a8c4);

    camera = new THREE.PerspectiveCamera(55, w / h, 0.01, 100000);
    camera.position.set(120, 90, 120);

    renderer = new THREE.WebGLRenderer({
      antialias: !performance || !!opts.sharp,
      powerPreference: 'high-performance',
      depth: true
    });
    renderer.setPixelRatio(opts.sharp ? Math.min(window.devicePixelRatio || 1, 3) : 1);
    renderer.setSize(w, h);
    renderer.sortObjects = false;
    if (THREE.sRGBEncoding !== undefined) {
      renderer.outputEncoding = THREE.sRGBEncoding;
    }
    container.appendChild(renderer.domElement);
    renderDirty = true;

    const amb = new THREE.AmbientLight(0xffffff, opts.freeOrbit ? 1.0 : 0.88);
    const dir = new THREE.DirectionalLight(0xffffff, opts.freeOrbit ? 0.22 : 0.28);
    dir.position.set(80, 120, 40);
    scene.add(amb, dir);

    controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = !performance;
    controls.dampingFactor = 0.08;
    controls.target.set(0, 0, 0);
    controls.minDistance = 0.05;
    controls.maxDistance = 50000;
    controls.zoomSpeed = 1.1;
    controls.screenSpacePanning = true;
    controls.enablePan = true;
    controls.minPolarAngle = opts.freeOrbit ? 0.02 : 0.08;
    controls.maxPolarAngle = opts.freeOrbit ? Math.PI - 0.02 : Math.PI / 2 - 0.04;
    controls.addEventListener('start', function () {
      if (isHeavyPointCloud()) beginPointLod();
    });
    controls.addEventListener('change', function () {
      requestRender();
      if (isHeavyPointCloud()) beginPointLod();
      else scheduleOsmRefresh();
    });
    controls.addEventListener('end', function () {
      if (isHeavyPointCloud()) endPointLod();
      else scheduleOsmRefresh();
    });
    renderer.domElement.addEventListener('wheel', onPointCloudWheel, { passive: true });

    resizeHandler = function () {
      if (!renderer || !camera || !container) return;
      const nw = container.clientWidth || window.innerWidth;
      const nh = container.clientHeight || window.innerHeight;
      camera.aspect = nw / nh;
      camera.updateProjectionMatrix();
      renderer.setSize(nw, nh);
      requestRender();
    };
    window.addEventListener('resize', resizeHandler);

    const loop = function () {
      animationId = requestAnimationFrame(loop);
      const damping = !!(controls && controls.enableDamping);
      if (damping) controls.update();
      if (contoursVisible && contourLabelData.length) {
        updateContourLabelsInView();
        renderDirty = true;
      }
      if ((renderDirty || damping) && renderer && scene && camera) {
        renderer.render(scene, camera);
        if (!damping) renderDirty = false;
      }
    };
    loop();
  }

  function requestRender() {
    renderDirty = true;
  }

  function isHeavyPointCloud() {
    return !!(lastPoints && lastPoints.shown > POINT_INTERACT_MAX);
  }

  function isPointCloudClose() {
    if (!camera || !controls) return false;
    const dist = camera.position.distanceTo(controls.target);
    if (pointFitDist && dist < pointFitDist * 0.22) return true;
    return dist < 12;
  }

  function setOsmVisible(on) {
    if (osmMesh) osmMesh.visible = !!on;
  }

  function applyFullChunkDraw() {
    if (!currentMesh || !currentMesh.isGroup) return;
    const children = currentMesh.children;
    for (let i = 0; i < children.length; i++) {
      const geom = children[i].geometry;
      if (!geom || !geom.getAttribute('position')) continue;
      geom.setDrawRange(0, geom.getAttribute('position').count);
    }
  }

  function setPointCloudLod(mode) {
    if (!currentMesh) return;
    if (mode === pointLodMode) {
      requestRender();
      return;
    }
    pointLodMode = mode;
    if (!isHeavyPointCloud()) {
      currentMesh.visible = true;
      if (previewMesh) previewMesh.visible = false;
      setOsmVisible(true);
      requestRender();
      return;
    }
    if (mode === 'preview') {
      currentMesh.visible = false;
      if (previewMesh) previewMesh.visible = true;
      setOsmVisible(false);
    } else {
      currentMesh.visible = true;
      if (previewMesh) previewMesh.visible = false;
      setOsmVisible(true);
      applyFullChunkDraw();
    }
    requestRender();
  }

  function beginPointLod() {
    if (!isHeavyPointCloud()) return;
    if (isPointCloudClose()) {
      setPointCloudLod('full');
      return;
    }
    pointInteracting = true;
    if (lodIdleTimer) {
      clearTimeout(lodIdleTimer);
      lodIdleTimer = null;
    }
    setPointCloudLod('preview');
  }

  function settlePointLod() {
    if (pointInteracting) return;
    setPointCloudLod('full');
    scheduleOsmRefresh();
  }

  function endPointLod() {
    if (!isHeavyPointCloud()) return;
    pointInteracting = false;
    if (lodIdleTimer) clearTimeout(lodIdleTimer);
    lodIdleTimer = setTimeout(function () {
      lodIdleTimer = null;
      if (!pointInteracting) settlePointLod();
    }, 380);
  }

  function onPointCloudWheel() {
    beginPointLod();
    endPointLod();
  }

  function ensurePreviewCloud() {
    if (previewMesh) {
      disposeObject3D(previewMesh);
      previewMesh = null;
    }
    if (!currentMesh || !scene || !isHeavyPointCloud()) return;
    const children = currentMesh.isGroup ? currentMesh.children : [currentMesh];
    const total = currentMesh.userData.totalPoints || 0;
    if (!total || total <= POINT_INTERACT_MAX) return;
    const budget = POINT_INTERACT_MAX;
    const p2 = new Float32Array(budget * 3);
    const c2 = new Uint8Array(budget * 3);
    let w = 0;
    for (let c = 0; c < children.length && w < budget; c++) {
      const geom = children[c].geometry;
      if (!geom) continue;
      const pos = geom.getAttribute('position');
      const col = geom.getAttribute('color');
      if (!pos) continue;
      const n = pos.count;
      const take = Math.max(1, Math.min(n, Math.ceil(n * budget / total)));
      const pa = pos.array;
      const ca = col ? col.array : null;
      const asByte = !!(ca && (col.normalized || ca instanceof Uint8Array));
      for (let i = 0; i < take && w < budget; i++) {
        const s = i * 3;
        const d = w * 3;
        p2[d] = pa[s];
        p2[d + 1] = pa[s + 1];
        p2[d + 2] = pa[s + 2];
        if (ca) {
          c2[d] = asByte ? ca[s] : Math.round(ca[s] * 255);
          c2[d + 1] = asByte ? ca[s + 1] : Math.round(ca[s + 1] * 255);
          c2[d + 2] = asByte ? ca[s + 2] : Math.round(ca[s + 2] * 255);
        }
        w += 1;
      }
    }
    if (!w) return;
    const geom = new THREE.BufferGeometry();
    const posOut = w === budget ? p2 : p2.slice(0, w * 3);
    const colOut = w === budget ? c2 : c2.slice(0, w * 3);
    geom.setAttribute('position', new THREE.BufferAttribute(posOut, 3));
    geom.setAttribute('color', new THREE.BufferAttribute(colOut, 3, true));
    previewMesh = new THREE.Points(geom, new THREE.PointsMaterial({
      size: lastPointAppearance.size || 2,
      vertexColors: lastPointAppearance.colorMode !== 'solid',
      color: lastPointAppearance.colorMode === 'solid' ? lastPointAppearance.color : 0xffffff,
      sizeAttenuation: false
    }));
    previewMesh.visible = false;
    previewMesh.frustumCulled = false;
    scene.add(previewMesh);
  }

  function decodeElevations(b64, width, height) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const elev = new Float32Array(bytes.buffer);
    if (elev.length !== width * height) {
      throw new Error('Kích thước elevations không khớp');
    }
    return elev;
  }

  function decodeMask(b64, width, height) {
    if (!b64) {
      const all = new Uint8Array(width * height);
      all.fill(1);
      return all;
    }
    const binary = atob(b64);
    const mask = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) mask[i] = binary.charCodeAt(i);
    if (mask.length !== width * height) {
      throw new Error('Kích thước mask không khớp');
    }
    return mask;
  }

  /**
   * Giải màu RGB theo cao độ (thấp → cao):
   * xanh dương → cyan → xanh lá → vàng → cam → đỏ
   */
  function heightColorRgb(t) {
    t = Math.max(0, Math.min(1, t));
    const stops = [
      { t: 0.0, r: 0.05, g: 0.2, b: 0.85 },
      { t: 0.2, r: 0.05, g: 0.75, b: 0.85 },
      { t: 0.4, r: 0.15, g: 0.85, b: 0.25 },
      { t: 0.6, r: 0.95, g: 0.9, b: 0.15 },
      { t: 0.8, r: 0.95, g: 0.45, b: 0.08 },
      { t: 1.0, r: 0.9, g: 0.1, b: 0.1 }
    ];

    let a = stops[0];
    let b = stops[stops.length - 1];
    for (let i = 0; i < stops.length - 1; i++) {
      if (t >= stops[i].t && t <= stops[i + 1].t) {
        a = stops[i];
        b = stops[i + 1];
        break;
      }
    }
    const u = (t - a.t) / Math.max(b.t - a.t, 1e-6);
    const c = new THREE.Color();
    c.setRGB(
      a.r + (b.r - a.r) * u,
      a.g + (b.g - a.g) * u,
      a.b + (b.b - a.b) * u
    );
    return c;
  }

  function loadTexture(url, opts) {
    opts = opts || {};
    return new Promise(function (resolve, reject) {
      new THREE.TextureLoader().load(
        url,
        function (tex) {
          if (THREE.sRGBEncoding !== undefined) {
            tex.encoding = THREE.sRGBEncoding;
          }
          let aniso = opts.anisotropy != null ? opts.anisotropy : (opts.sharp ? 16 : 4);
          if (renderer && renderer.capabilities && renderer.capabilities.getMaxAnisotropy) {
            aniso = opts.sharp
              ? renderer.capabilities.getMaxAnisotropy()
              : Math.min(aniso, renderer.capabilities.getMaxAnisotropy());
          }
          tex.anisotropy = aniso;
          if (opts.sharp) {
            tex.generateMipmaps = true;
            tex.minFilter = THREE.LinearMipmapLinearFilter;
            tex.magFilter = THREE.LinearFilter;
          }
          tex.needsUpdate = true;
          resolve(tex);
        },
        undefined,
        reject
      );
    });
  }

  function texelSizeFromMap(map) {
    const img = map && map.image;
    const w = (img && (img.naturalWidth || img.width)) || 8192;
    const h = (img && (img.naturalHeight || img.height)) || 8192;
    return new THREE.Vector2(1 / Math.max(1, w), 1 / Math.max(1, h));
  }

  function applySharpMeshMaterial(mat, map) {
    if (!mat || !map) return mat;
    const texel = texelSizeFromMap(map);
    mat.onBeforeCompile = function (shader) {
      shader.uniforms.uTexel = { value: texel };
      shader.fragmentShader =
        'uniform vec2 uTexel;\n' +
        shader.fragmentShader.replace(
          'vec4 texelColor = texture2D( map, vUv );',
          [
            'vec4 texelColor = texture2D( map, vUv, -2.25 );',
            'vec4 blurCol = (',
            '  texture2D( map, vUv + vec2(uTexel.x, 0.0), -2.25 ) +',
            '  texture2D( map, vUv - vec2(uTexel.x, 0.0), -2.25 ) +',
            '  texture2D( map, vUv + vec2(0.0, uTexel.y), -2.25 ) +',
            '  texture2D( map, vUv - vec2(0.0, uTexel.y), -2.25 )',
            ') * 0.25;',
            'texelColor = clamp(texelColor + (texelColor - blurCol) * 0.88, 0.0, 1.0);'
          ].join('\n\t')
        );
    };
    mat.customProgramCacheKey = function () {
      return 'mesh-obj-sharp-v3';
    };
    return mat;
  }

  async function applyTextureUrl(url) {
    if (!currentMesh || !url) return false;
    const tex = await loadTexture(url);
    tex.flipY = true;
    tex.needsUpdate = true;
    const material = new THREE.MeshBasicMaterial({
      map: tex,
      side: THREE.DoubleSide,
      transparent: true,
      alphaTest: 0.05,
      depthWrite: true
    });
    applySharpMeshMaterial(material, tex);
    currentMesh.traverse(function (obj) {
      if (!obj.isMesh) return;
      if (obj.material) {
        const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
        mats.forEach(function (m) {
          if (m && m.map && m.map !== tex) m.map.dispose();
          if (m) m.dispose();
        });
      }
      obj.material = material;
    });
    return true;
  }

  const WEB_MERCATOR_MAX = 20037508.342789244;

  function parseDemBounds(bounds) {
    if (!bounds) return null;
    let west;
    let south;
    let east;
    let north;
    if (Array.isArray(bounds)) {
      west = bounds[0];
      south = bounds[1];
      east = bounds[2];
      north = bounds[3];
    } else {
      west = bounds.west;
      south = bounds.south;
      east = bounds.east;
      north = bounds.north;
    }
    if (![west, south, east, north].every(function (v) { return Number.isFinite(v); })) {
      return null;
    }
    return {
      west: west,
      south: south,
      east: east,
      north: north,
      lonSpan: Math.max(east - west, 1e-9),
      latSpan: Math.max(north - south, 1e-9),
      lonCenter: (west + east) / 2,
      latCenter: (south + north) / 2
    };
  }

  /** Bounds CRS gốc raster: {left,bottom,right,top} hoặc [left,bottom,right,top]. */
  function parseNativeBounds(bounds) {
    if (!bounds) return null;
    let left;
    let bottom;
    let right;
    let top;
    if (Array.isArray(bounds)) {
      left = bounds[0];
      bottom = bounds[1];
      right = bounds[2];
      top = bounds[3];
    } else {
      left = bounds.left;
      bottom = bounds.bottom;
      right = bounds.right;
      top = bounds.top;
    }
    if (![left, bottom, right, top].every(function (v) { return Number.isFinite(Number(v)); })) {
      return null;
    }
    left = Number(left);
    bottom = Number(bottom);
    right = Number(right);
    top = Number(top);
    const xSpan = Math.max(Math.abs(right - left), 1e-9);
    const ySpan = Math.max(Math.abs(top - bottom), 1e-9);
    return {
      left: left,
      bottom: bottom,
      right: right,
      top: top,
      xSpan: xSpan,
      ySpan: ySpan,
      xCenter: (left + right) / 2,
      yCenter: (bottom + top) / 2
    };
  }

  function isWebMercatorCrs(crs, nativeBounds) {
    if (crs) {
      const s = String(crs).toUpperCase();
      if (
        s.indexOf('3857') !== -1 ||
        s.indexOf('900913') !== -1 ||
        s.indexOf('WEB MERCATOR') !== -1 ||
        s.indexOf('PSEUDO-MERCATOR') !== -1 ||
        s.indexOf('POPULAR VISUALISATION') !== -1
      ) {
        return true;
      }
    }
    // Heuristic: bounds theo mét Web Mercator (không phải độ)
    if (nativeBounds) {
      const maxAbs = Math.max(
        Math.abs(nativeBounds.left),
        Math.abs(nativeBounds.right),
        Math.abs(nativeBounds.bottom),
        Math.abs(nativeBounds.top)
      );
      return maxAbs > 180;
    }
    return false;
  }

  function isGeographicCrs(crs) {
    if (!crs) return false;
    const s = String(crs).toUpperCase().trim();
    // Tránh false-positive với WKT Web Mercator có chuỗi "WGS 84" trong base CRS
    if (isWebMercatorCrs(s, null)) return false;
    if (s === 'EPSG:4326' || s === '4326') return true;
    if (s.indexOf('EPSG:4326') !== -1) return true;
    if (s.indexOf('GEOGCRS') !== -1 || s.indexOf('GEOGCS') !== -1) return true;
    return false;
  }

  function lonLatToWebMercator(lon, lat) {
    const clampedLat = Math.max(Math.min(lat, 85.05112878), -85.05112878);
    const x = (lon * WEB_MERCATOR_MAX) / 180;
    let y = Math.log(Math.tan(((90 + clampedLat) * Math.PI) / 360)) / (Math.PI / 180);
    y = (y * WEB_MERCATOR_MAX) / 180;
    return { x: x, y: y };
  }

  function demWorldSize(terrain) {
    const width = Math.max(1, Number(terrain.width) || 1);
    const height = Math.max(1, Number(terrain.height) || 1);
    const worldW = DEM_WORLD_W;
    // Ưu tiên tỉ lệ mét CRS gốc (khớp footprint địa lý); fallback theo pixel grid
    let aspect = height / width;
    const native = parseNativeBounds(terrain.bounds);
    if (native) {
      aspect = native.ySpan / native.xSpan;
    } else {
      const wgs = parseDemBounds(terrain.bounds_wgs84);
      if (wgs && !isGeographicCrs(terrain.crs)) {
        // Ước lượng tỉ lệ mercator từ bbox WGS84 khi thiếu bounds gốc
        const sw = lonLatToWebMercator(wgs.west, wgs.south);
        const ne = lonLatToWebMercator(wgs.east, wgs.north);
        const xSpan = Math.max(Math.abs(ne.x - sw.x), 1e-9);
        const ySpan = Math.max(Math.abs(ne.y - sw.y), 1e-9);
        aspect = ySpan / xSpan;
      }
    }
    return { worldW: worldW, worldH: worldW * aspect };
  }

  function elevationScaleForTerrain(terrain, worldW) {
    const zmin = Number(terrain.zmin);
    const zmax = Number(terrain.zmax);
    const zrange = Math.max(zmax - zmin, 1e-6);
    return (worldW / zrange) * exaggeration;
  }

  /** Map toạ độ phẳng (CRS DEM hoặc Web Mercator) → thế giới Three.js. */
  function projToWorld(px, py, projBounds, worldW, worldH) {
    const x = ((px - projBounds.xCenter) / projBounds.xSpan) * worldW;
    const z = -((py - projBounds.yCenter) / projBounds.ySpan) * worldH;
    return { x: x, z: z };
  }

  /** Lon/lat → thế giới; dùng mercator khi DEM là Web Mercator / projected. */
  function lonLatToWorld(lon, lat, terrain, worldW, worldH) {
    const native = parseNativeBounds(terrain && terrain.bounds);
    const wgs = parseDemBounds(terrain && terrain.bounds_wgs84);

    if (native && isWebMercatorCrs(terrain.crs, native)) {
      const m = lonLatToWebMercator(lon, lat);
      return projToWorld(m.x, m.y, native, worldW, worldH);
    }

    if (native && isGeographicCrs(terrain.crs)) {
      return projToWorld(lon, lat, native, worldW, worldH);
    }

    // DEM projected khác 3857 nhưng có WGS84: căn OSM theo mercator
    // của footprint WGS84 (khớp tile OSM), mesh vẫn theo pixel/native aspect.
    if (wgs && !isGeographicCrs(terrain && terrain.crs)) {
      const sw = lonLatToWebMercator(wgs.west, wgs.south);
      const ne = lonLatToWebMercator(wgs.east, wgs.north);
      const mercBounds = {
        left: Math.min(sw.x, ne.x),
        right: Math.max(sw.x, ne.x),
        bottom: Math.min(sw.y, ne.y),
        top: Math.max(sw.y, ne.y),
        xSpan: Math.max(Math.abs(ne.x - sw.x), 1e-9),
        ySpan: Math.max(Math.abs(ne.y - sw.y), 1e-9),
        xCenter: (sw.x + ne.x) / 2,
        yCenter: (sw.y + ne.y) / 2
      };
      const m = lonLatToWebMercator(lon, lat);
      return projToWorld(m.x, m.y, mercBounds, worldW, worldH);
    }

    // Fallback: nội suy tuyến tính lon/lat (DEM geographic)
    if (!wgs) return { x: 0, z: 0 };
    const x = ((lon - wgs.lonCenter) / wgs.lonSpan) * worldW;
    const z = -((lat - wgs.latCenter) / wgs.latSpan) * worldH;
    return { x: x, z: z };
  }

  function lonLatToTileXY(lon, lat, z) {
    const n = Math.pow(2, z);
    const clampedLat = Math.max(Math.min(lat, 85.05112878), -85.05112878);
    const x = ((lon + 180) / 360) * n;
    const latRad = (clampedLat * Math.PI) / 180;
    const y =
      (1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2 * n;
    return { x: x, y: y };
  }

  function tileToLonLatBounds(z, x, y) {
    const n = Math.pow(2, z);
    const west = (x / n) * 360 - 180;
    const east = ((x + 1) / n) * 360 - 180;
    const north =
      (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / n))) * 180) / Math.PI;
    const south =
      (Math.atan(Math.sinh(Math.PI * (1 - (2 * (y + 1)) / n))) * 180) / Math.PI;
    return { west: west, south: south, east: east, north: north };
  }

  function osmTileRange(bounds, z) {
    const n = Math.pow(2, z);
    const nw = lonLatToTileXY(bounds.west, bounds.north, z);
    const se = lonLatToTileXY(bounds.east, bounds.south, z);
    let x0 = Math.floor(nw.x) - OSM_MARGIN_TILES;
    let y0 = Math.floor(nw.y) - OSM_MARGIN_TILES;
    let x1 = Math.floor(se.x) + OSM_MARGIN_TILES;
    let y1 = Math.floor(se.y) + OSM_MARGIN_TILES;
    x0 = Math.max(0, x0);
    y0 = Math.max(0, y0);
    x1 = Math.min(n - 1, x1);
    y1 = Math.min(n - 1, y1);
    const cols = Math.max(0, x1 - x0 + 1);
    const rows = Math.max(0, y1 - y0 + 1);
    return { x0: x0, y0: y0, x1: x1, y1: y1, cols: cols, rows: rows, count: cols * rows, n: n };
  }

  function chooseOsmZoom(demBounds) {
    for (let z = OSM_NATIVE_MAX_ZOOM; z >= 6; z--) {
      if (osmTileRange(demBounds, z).count <= 36) return z;
    }
    return 8;
  }

  function osmGroundViewBounds(terrain) {
    const demBounds = parseDemBounds(terrain.bounds_wgs84);
    if (!demBounds) return null;
    const sizes = demWorldSize(terrain);
    const worldW = sizes.worldW;
    const worldH = sizes.worldH;
    const maxSpan = Math.max(worldW, worldH, 1) * 4;
    if (!camera) {
      return { west: demBounds.west, south: demBounds.south, east: demBounds.east, north: demBounds.north };
    }
    const hit = new THREE.Vector3();
    const ndc = new THREE.Vector2();
    const raycaster = new THREE.Raycaster();
    const samples = [
      [-1, -1], [1, -1], [-1, 1], [1, 1],
      [-1, 0], [1, 0], [0, -1], [0, 1], [0, 0]
    ];
    const xs = [];
    const zs = [];
    for (let i = 0; i < samples.length; i++) {
      ndc.set(samples[i][0], samples[i][1]);
      raycaster.setFromCamera(ndc, camera);
      const ray = raycaster.ray;
      if (Math.abs(ray.direction.y) < 1e-5) continue;
      const t = -(ray.origin.y + 0.35) / ray.direction.y;
      if (!(t > 0) || t > maxSpan * 8) continue;
      hit.copy(ray.origin).addScaledVector(ray.direction, t);
      if (!Number.isFinite(hit.x) || !Number.isFinite(hit.z)) continue;
      if (Math.abs(hit.x) > maxSpan || Math.abs(hit.z) > maxSpan) continue;
      xs.push(hit.x);
      zs.push(hit.z);
    }
    let west;
    let east;
    let south;
    let north;
    if (xs.length >= 3) {
      const x0 = Math.min.apply(null, xs);
      const x1 = Math.max.apply(null, xs);
      const z0 = Math.min.apply(null, zs);
      const z1 = Math.max.apply(null, zs);
      const a = worldToLonLat(x0, z0, terrain);
      const b = worldToLonLat(x1, z1, terrain);
      west = Math.min(a.lon, b.lon);
      east = Math.max(a.lon, b.lon);
      south = Math.min(a.lat, b.lat);
      north = Math.max(a.lat, b.lat);
    } else {
      west = demBounds.west;
      east = demBounds.east;
      south = demBounds.south;
      north = demBounds.north;
    }
    const padLon = Math.max((east - west) * 0.22, demBounds.lonSpan * 0.02, 1e-5);
    const padLat = Math.max((north - south) * 0.22, demBounds.latSpan * 0.02, 1e-5);
    west -= padLon;
    east += padLon;
    south -= padLat;
    north += padLat;
    const lonLimit = demBounds.lonSpan * 2.5;
    const latLimit = demBounds.latSpan * 2.5;
    if (east - west > lonLimit) {
      const mid = (west + east) / 2;
      west = mid - lonLimit / 2;
      east = mid + lonLimit / 2;
    }
    if (north - south > latLimit) {
      const mid = (south + north) / 2;
      south = mid - latLimit / 2;
      north = mid + latLimit / 2;
    }
    return { west: west, south: south, east: east, north: north };
  }

  function osmViewWindow(terrain) {
    const demBounds = parseDemBounds(terrain.bounds_wgs84);
    if (!demBounds) return null;
    const zFit = chooseOsmZoom(demBounds);
    const bounds = osmGroundViewBounds(terrain) || demBounds;
    let z = OSM_NATIVE_MAX_ZOOM;
    if (camera && controls) {
      const sizes = demWorldSize(terrain);
      const dist = camera.position.distanceTo(controls.target);
      const fit = Math.max(sizes.worldW, sizes.worldH, 1) * 1.15;
      z = Math.max(
        6,
        Math.min(OSM_NATIVE_MAX_ZOOM, Math.round(zFit + Math.log2(fit / Math.max(dist, 1e-3)) + 1))
      );
    }
    while (z > 6 && osmTileRange(bounds, z).count > OSM_MAX_TILES) {
      z -= 1;
    }
    return { bounds: bounds, z: z };
  }

  function scheduleOsmRefresh() {
    if (!lastTerrain || !scene || pointInteracting) return;
    if (osmRefreshTimer) clearTimeout(osmRefreshTimer);
    osmRefreshTimer = setTimeout(function () {
      osmRefreshTimer = null;
      refreshOsmBasemap(lastTerrain);
    }, 220);
  }

  async function refreshOsmBasemap(terrain) {
    if (!terrain || !scene || lastTerrain !== terrain) return;
    const win = osmViewWindow(terrain);
    if (!win || !win.bounds) return;
    const key = [win.z, win.bounds.west.toFixed(5), win.bounds.south.toFixed(5),
      win.bounds.east.toFixed(5), win.bounds.north.toFixed(5)].join(':');
    if (key === lastOsmKey && osmMesh) return;
    const buildId = ++osmBuildId;
    const mesh = await buildOsmBasemap(terrain, win);
    if (buildId !== osmBuildId || lastTerrain !== terrain || !scene) {
      if (mesh) disposeObject3D(mesh);
      return;
    }
    lastOsmKey = key;
    disposeObject3D(osmMesh);
    osmMesh = mesh;
    if (osmMesh) {
      osmMesh.visible = pointLodMode !== 'preview';
      scene.add(osmMesh);
    }
    requestRender();
  }

  /**
   * Lưới tile OSM Standard (proxy → tile.openstreetmap.org).
   * Đặt tile theo Web Mercator / CRS DEM — khớp mesh Three.js (không nội suy lon/lat thuần).
   */
  async function buildOsmBasemap(terrain, viewWin) {
    const demBounds = parseDemBounds(terrain.bounds_wgs84);
    if (!demBounds) return null;
    const win = viewWin || osmViewWindow(terrain) || { bounds: demBounds, z: chooseOsmZoom(demBounds) };
    const tileBounds = win.bounds || demBounds;
    const z = win.z || chooseOsmZoom(demBounds);

    const dem = demWorldSize(terrain);
    let range = osmTileRange(tileBounds, z);
    let useZ = z;
    while (useZ > 6 && range.count > OSM_MAX_TILES) {
      useZ -= 1;
      range = osmTileRange(tileBounds, useZ);
    }
    const x0 = range.x0;
    const y0 = range.y0;
    const x1 = range.x1;
    const y1 = range.y1;

    const group = new THREE.Group();
    group.name = 'osmTiles';

    const jobs = [];
    for (let ty = y0; ty <= y1; ty++) {
      for (let tx = x0; tx <= x1; tx++) {
        jobs.push({ tx: tx, ty: ty, z: useZ });
      }
    }

    async function addTile(job) {
      const tileZ = job.z;
      const b = tileToLonLatBounds(tileZ, job.tx, job.ty);
      const sw = lonLatToWorld(b.west, b.south, terrain, dem.worldW, dem.worldH);
      const ne = lonLatToWorld(b.east, b.north, terrain, dem.worldW, dem.worldH);
      const w = Math.abs(ne.x - sw.x);
      const h = Math.abs(sw.z - ne.z);
      const cx = (sw.x + ne.x) / 2;
      const cz = (sw.z + ne.z) / 2;

      const url = '/api/basemap/osm/' + tileZ + '/' + job.tx + '/' + job.ty + '.png';
      let tex;
      try {
        tex = await loadTexture(url);
      } catch (e) {
        return;
      }

      const geometry = new THREE.PlaneGeometry(Math.max(w, 1e-3), Math.max(h, 1e-3));
      geometry.rotateX(-Math.PI / 2);
      // OSM PNG: hàng đầu = Bắc; flipY mặc định của Three khớp PlaneGeometry (+Y → Bắc/−Z)
      tex.flipY = true;
      const material = new THREE.MeshBasicMaterial({
        map: tex,
        side: THREE.DoubleSide,
        depthWrite: false,
        polygonOffset: true,
        polygonOffsetFactor: 1,
        polygonOffsetUnits: 1
      });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.set(cx, -0.35, cz);
      group.add(mesh);
    }

    const concurrency = 4;
    for (let i = 0; i < jobs.length; i += concurrency) {
      const batch = jobs.slice(i, i + concurrency);
      await Promise.all(batch.map(addTile));
    }

    if (!group.children.length) return null;
    return group;
  }

  async function buildTerrainMesh(terrain) {
    const width = terrain.width;
    const height = terrain.height;
    const elev = decodeElevations(terrain.elevations_b64, width, height);
    const mask = decodeMask(terrain.mask_b64, width, height);
    const zmin = terrain.zmin;
    const zmax = terrain.zmax;
    const zrange = Math.max(zmax - zmin, 1e-6);

    const sizes = demWorldSize(terrain);
    const worldW = sizes.worldW;
    const worldH = sizes.worldH;
    const geometry = new THREE.PlaneGeometry(worldW, worldH, width - 1, height - 1);
    geometry.rotateX(-Math.PI / 2);

    const positions = geometry.attributes.position;
    const colors = new Float32Array(positions.count * 4);
    const elevScale = elevationScaleForTerrain(terrain, worldW);

    const valid = new Uint8Array(positions.count);
    // PlaneGeometry (r128): iy=0 → +Y → sau rotateX(-π/2) thành −Z (Bắc).
    // GeoTIFF north-up: row 0 = Bắc → map thẳng iy == rasterRow (không đảo N-S).
    for (let iy = 0; iy < height; iy++) {
      for (let ix = 0; ix < width; ix++) {
        const rasterRow = iy;
        const idx = iy * width + ix;
        const rasterIdx = rasterRow * width + ix;
        const isValid = mask[rasterIdx] ? 1 : 0;
        valid[idx] = isValid;
        const z = elev[rasterIdx];
        const t = (z - zmin) / zrange;
        if (isValid) {
          const y = terrain.is_dem ? (z - zmin) * elevScale : 0.12;
          positions.setY(idx, y);
        } else {
          positions.setXYZ(idx, NaN, NaN, NaN);
        }

        const col = heightColorRgb(t);
        colors[idx * 4] = col.r;
        colors[idx * 4 + 1] = col.g;
        colors[idx * 4 + 2] = col.b;
        colors[idx * 4 + 3] = isValid ? 1 : 0;
      }
    }
    positions.needsUpdate = true;
    geometry.setAttribute('color', new THREE.BufferAttribute(colors, 4));

    const indices = [];
    for (let iy = 0; iy < height - 1; iy++) {
      for (let ix = 0; ix < width - 1; ix++) {
        const a = iy * width + ix;
        const b = a + 1;
        const c = a + width;
        const d = c + 1;
        if (valid[a] && valid[b] && valid[c] && valid[d]) {
          indices.push(a, c, b);
          indices.push(b, c, d);
        }
      }
    }
    if (indices.length) {
      geometry.setIndex(indices);
    } else {
      geometry.setIndex([]);
    }
    geometry.computeVertexNormals();

    let material;
    if (terrain.is_dem) {
      material = new THREE.MeshStandardMaterial({
        vertexColors: true,
        roughness: 0.88,
        metalness: 0.04,
        side: THREE.DoubleSide,
        transparent: true,
        alphaTest: 0.5,
        depthWrite: true
      });
    } else {
      try {
        const tex = await loadTexture(terrain.texture_url);
        material = new THREE.MeshStandardMaterial({
          map: tex,
          roughness: 0.95,
          metalness: 0.05,
          side: THREE.DoubleSide,
          transparent: true,
          alphaTest: 0.05,
          depthWrite: true
        });
      } catch (e) {
        material = new THREE.MeshStandardMaterial({
          vertexColors: true,
          roughness: 0.92,
          metalness: 0.05,
          side: THREE.DoubleSide,
          transparent: true,
          alphaTest: 0.5
        });
      }
    }

    return new THREE.Mesh(geometry, material);
  }

  /** Mục tiêu ~50 đường contour theo (zmax − zmin); cứ 5 đường → 1 nét liền + nhãn. */
  const CONTOUR_TARGET_COUNT = 50;
  const CONTOUR_MAJOR_EVERY = 5;
  /** Lưới tối đa khi trích contour — khớp độ phân giải mesh DEM (~256). */
  const CONTOUR_GRID_MAX = 256;
  /** Nét liền (major) dày hơn nét đứt */
  const CONTOUR_LINE_WIDTH = 2.5;
  /** Nét đứt: ngắn + mỏng hơn nét liền */
  const CONTOUR_DASH_LINE_WIDTH = 0.55;
  const CONTOUR_DASH_SIZE = 0.7;
  const CONTOUR_GAP_SIZE = 0.55;

  /** Chọn khoảng cao “đẹp” để số đường gần targetCount. */
  function niceContourStep(range, targetCount) {
    const rough = range / Math.max(targetCount, 1);
    if (!(rough > 0) || !Number.isFinite(rough)) return 1;
    const exp = Math.floor(Math.log10(rough));
    const pow10 = Math.pow(10, exp);
    const candidates = [1, 2, 2.5, 5, 10].map(function (m) {
      return m * pow10;
    });
    candidates.push(0.5 * pow10, 20 * pow10);
    let best = candidates[0];
    let bestScore = Infinity;
    candidates.forEach(function (c) {
      if (!(c > 0)) return;
      const n = range / c;
      const score = Math.abs(n - targetCount);
      if (score < bestScore) {
        bestScore = score;
        best = c;
      }
    });
    return best;
  }

  function contourLevels(zmin, zmax) {
    const range = Math.max(zmax - zmin, 1e-6);
    const step = niceContourStep(range, CONTOUR_TARGET_COUNT);
    const majorStep = step * CONTOUR_MAJOR_EVERY;
    const start = Math.ceil(zmin / step) * step;
    const levels = [];
    const maxLevels = CONTOUR_TARGET_COUNT * 2;
    for (let z = start; z <= zmax + step * 1e-9; z += step) {
      if (z < zmin + 1e-6 || z > zmax - 1e-6) continue;
      levels.push(Math.round(z * 1e6) / 1e6);
      if (levels.length >= maxLevels) break;
    }
    return {
      levels: levels,
      step: step,
      labelStep: majorStep,
      solidStep: majorStep
    };
  }

  function isContourLabelLevel(level, labelStep) {
    if (!(labelStep > 0)) return false;
    const q = level / labelStep;
    return Math.abs(q - Math.round(q)) < 1e-6;
  }

  /** Downsample DEM để trích contour nhanh hơn. */
  function downsampleContourGrid(elev, mask, width, height, maxDim) {
    const scale = Math.max(width, height) / Math.max(maxDim, 2);
    if (scale <= 1.01) {
      return { elev: elev, mask: mask, width: width, height: height };
    }
    const nw = Math.max(2, Math.round(width / scale));
    const nh = Math.max(2, Math.round(height / scale));
    const ne = new Float32Array(nw * nh);
    const nm = new Uint8Array(nw * nh);
    const yScale = (height - 1) / Math.max(nh - 1, 1);
    const xScale = (width - 1) / Math.max(nw - 1, 1);
    for (let r = 0; r < nh; r++) {
      const sr = Math.min(height - 1, Math.round(r * yScale));
      for (let c = 0; c < nw; c++) {
        const sc = Math.min(width - 1, Math.round(c * xScale));
        const si = sr * width + sc;
        const di = r * nw + c;
        ne[di] = elev[si];
        nm[di] = mask[si];
      }
    }
    return { elev: ne, mask: nm, width: nw, height: nh };
  }

  /**
   * Marching squares nhanh: chỉ trả đoạn cạnh (không assemble polyline).
   * Mỗi đoạn: r0,c0,r1,c1 trong mảng phẳng segs.
   */
  function findContourSegments(data, width, height, level, mask) {
    const segs = [];
    const h1 = height - 1;
    const w1 = width - 1;

    for (let r = 0; r < h1; r++) {
      const row0 = r * width;
      const row1 = (r + 1) * width;
      for (let c = 0; c < w1; c++) {
        const i00 = row0 + c;
        const i10 = row0 + c + 1;
        const i01 = row1 + c;
        const i11 = row1 + c + 1;
        if (!mask[i00] || !mask[i10] || !mask[i01] || !mask[i11]) continue;

        const v00 = data[i00];
        const v10 = data[i10];
        const v01 = data[i01];
        const v11 = data[i11];
        if (
          !Number.isFinite(v00) ||
          !Number.isFinite(v10) ||
          !Number.isFinite(v01) ||
          !Number.isFinite(v11)
        ) {
          continue;
        }

        let idx = 0;
        if (v00 > level) idx |= 1;
        if (v10 > level) idx |= 2;
        if (v11 > level) idx |= 4;
        if (v01 > level) idx |= 8;
        if (idx === 0 || idx === 15) continue;

        // Điểm trên cạnh: top=0, right=1, bottom=2, left=3
        let tr, tc, rr, rc, br, bc, lr, lc;
        {
          let t = v10 === v00 ? 0.5 : (level - v00) / (v10 - v00);
          t = t < 0 ? 0 : t > 1 ? 1 : t;
          tr = r; tc = c + t;
        }
        {
          let t = v11 === v10 ? 0.5 : (level - v10) / (v11 - v10);
          t = t < 0 ? 0 : t > 1 ? 1 : t;
          rr = r + t; rc = c + 1;
        }
        {
          let t = v11 === v01 ? 0.5 : (level - v01) / (v11 - v01);
          t = t < 0 ? 0 : t > 1 ? 1 : t;
          br = r + 1; bc = c + t;
        }
        {
          let t = v01 === v00 ? 0.5 : (level - v00) / (v01 - v00);
          t = t < 0 ? 0 : t > 1 ? 1 : t;
          lr = r + t; lc = c;
        }

        switch (idx) {
          case 1: case 14: segs.push(lr, lc, tr, tc); break;
          case 2: case 13: segs.push(tr, tc, rr, rc); break;
          case 3: case 12: segs.push(lr, lc, rr, rc); break;
          case 4: case 11: segs.push(rr, rc, br, bc); break;
          case 6: case 9: segs.push(tr, tc, br, bc); break;
          case 7: case 8: segs.push(lr, lc, br, bc); break;
          case 5:
            segs.push(tr, tc, lr, lc, rr, rc, br, bc);
            break;
          case 10:
            segs.push(tr, tc, rr, rc, lr, lc, br, bc);
            break;
          default:
            break;
        }
      }
    }
    return segs;
  }

  /** Nội suy bilinear cao độ trên lưới DEM đầy đủ (cùng công thức mesh). */
  function sampleElevBilinear(elev, mask, width, height, row, col) {
    const r0 = Math.max(0, Math.min(height - 1, Math.floor(row)));
    const c0 = Math.max(0, Math.min(width - 1, Math.floor(col)));
    const r1 = Math.min(height - 1, r0 + 1);
    const c1 = Math.min(width - 1, c0 + 1);
    const fr = Math.max(0, Math.min(1, row - r0));
    const fc = Math.max(0, Math.min(1, col - c0));

    const i00 = r0 * width + c0;
    const i10 = r0 * width + c1;
    const i01 = r1 * width + c0;
    const i11 = r1 * width + c1;

    const v00 = elev[i00];
    const v10 = elev[i10];
    const v01 = elev[i01];
    const v11 = elev[i11];
    const m00 = mask[i00];
    const m10 = mask[i10];
    const m01 = mask[i01];
    const m11 = mask[i11];

    let sum = 0;
    let wsum = 0;
    function accum(v, m, w) {
      if (!m || !Number.isFinite(v) || !(w > 0)) return;
      sum += v * w;
      wsum += w;
    }
    accum(v00, m00, (1 - fr) * (1 - fc));
    accum(v10, m10, (1 - fr) * fc);
    accum(v01, m01, fr * (1 - fc));
    accum(v11, m11, fr * fc);
    if (wsum > 1e-9) return sum / wsum;
    if (m00 && Number.isFinite(v00)) return v00;
    return NaN;
  }

  /**
   * Raster (row,col) lưới contour → world.
   * XZ theo footprint mesh; Y draping theo cao độ DEM tại điểm đó (bám sát mặt mesh).
   */
  function contourRasterToWorld(
    row, col, gridW, gridH, fullW, fullH,
    worldW, worldH, zmin, elevScale, level,
    elevFull, maskFull
  ) {
    const colFull = col * (fullW - 1) / Math.max(gridW - 1, 1);
    const rowFull = row * (fullH - 1) / Math.max(gridH - 1, 1);
    const dx = Math.max(fullW - 1, 1);
    const dy = Math.max(fullH - 1, 1);
    // row 0 = Bắc = −Z; khớp mesh + OSM (EPSG:3857)
    const x = -worldW / 2 + (colFull / dx) * worldW;
    const z = -worldH / 2 + (rowFull / dy) * worldH;

    let elevVal = sampleElevBilinear(elevFull, maskFull, fullW, fullH, rowFull, colFull);
    if (!Number.isFinite(elevVal)) elevVal = level;
    // Nhấc rất nhẹ khỏi mặt để tránh z-fighting, vẫn bám mesh
    const lift = Math.max(worldW, worldH) * 0.00012 + 0.008;
    const y = (elevVal - zmin) * elevScale + lift;
    return { x: x, y: y, z: z };
  }

  /** Cắt đoạn thành nét đứt, ghi vào mảng LineSegments. */
  function appendDashedWorldSegment(out, ax, ay, az, bx, by, bz, dashSize, gapSize) {
    const dx = bx - ax;
    const dy = by - ay;
    const dz = bz - az;
    const len = Math.sqrt(dx * dx + dy * dy + dz * dz);
    if (!(len > 1e-8)) return;
    const inv = 1 / len;
    const ux = dx * inv;
    const uy = dy * inv;
    const uz = dz * inv;
    let t = 0;
    let drawing = true;
    while (t < len) {
      const span = drawing ? dashSize : gapSize;
      const t1 = t + span < len ? t + span : len;
      if (drawing && t1 > t) {
        out.push(
          ax + ux * t, ay + uy * t, az + uz * t,
          ax + ux * t1, ay + uy * t1, az + uz * t1
        );
      }
      t = t1;
      drawing = !drawing;
    }
  }

  function makeElevLabelSprite(text) {
    const canvas = document.createElement('canvas');
    canvas.width = 128;
    canvas.height = 48;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.font = 'bold 28px Arial, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 4;
    ctx.strokeStyle = 'rgba(255,255,255,0.85)';
    ctx.strokeText(text, 64, 24);
    ctx.fillStyle = '#e30613';
    ctx.fillText(text, 64, 24);

    const tex = new THREE.CanvasTexture(canvas);
    tex.needsUpdate = true;
    const mat = new THREE.SpriteMaterial({
      map: tex,
      transparent: true,
      // Không depth-test → DEM không che chữ; vị trí vẫn bám contour
      depthTest: false,
      depthWrite: false,
      sizeAttenuation: true
    });
    const sprite = new THREE.Sprite(mat);
    sprite.scale.set(4.5, 1.7, 1);
    sprite.center.set(0.5, 0.5);
    sprite.renderOrder = 999;
    sprite.userData.labelText = text;
    return sprite;
  }

  function formatContourLabel(z) {
    return String(Math.round(z));
  }

  function clearContourLabels() {
    if (contourLabelLayer) {
      disposeObject3D(contourLabelLayer);
      contourLabelLayer = null;
    }
    contourLabelSprites = [];
    contourLabelData = [];
    labelsNeedUpdate = true;
  }

  function setLabelSpriteText(sprite, text) {
    if (sprite.userData.labelText === text) return;
    const canvas = document.createElement('canvas');
    canvas.width = 128;
    canvas.height = 48;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.font = 'bold 28px Arial, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 4;
    ctx.strokeStyle = 'rgba(255,255,255,0.85)';
    ctx.strokeText(text, 64, 24);
    ctx.fillStyle = '#e30613';
    ctx.fillText(text, 64, 24);
    const tex = new THREE.CanvasTexture(canvas);
    tex.needsUpdate = true;
    if (sprite.material && sprite.material.map) sprite.material.map.dispose();
    if (sprite.material) {
      sprite.material.map = tex;
      sprite.material.needsUpdate = true;
    }
    sprite.userData.labelText = text;
  }

  let _lastLabelCamKey = '';

  /**
   * Chỉ hiện nhãn nằm trong khung nhìn hiện tại (zoom/pan).
   * Giới hạn số nhãn + khoảng cách màn hình để tránh chồng.
   */
  function updateContourLabelsInView() {
    if (!camera || !scene || !contourLabelData.length) return;

    // Chỉ tính lại khi camera đổi (hoặc lần đầu)
    const camKey =
      camera.position.x.toFixed(2) +
      ',' +
      camera.position.y.toFixed(2) +
      ',' +
      camera.position.z.toFixed(2) +
      ',' +
      (controls ? controls.target.x.toFixed(2) : '0') +
      ',' +
      (controls ? controls.target.y.toFixed(2) : '0') +
      ',' +
      (controls ? controls.target.z.toFixed(2) : '0');
    if (!labelsNeedUpdate && camKey === _lastLabelCamKey) return;
    _lastLabelCamKey = camKey;

    camera.updateMatrixWorld();
    _labelProj.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse);
    _labelFrustum.setFromProjectionMatrix(_labelProj);

    const inView = [];
    for (let i = 0; i < contourLabelData.length; i++) {
      const c = contourLabelData[i];
      _labelWorld.set(c.x, c.y, c.z);
      if (!_labelFrustum.containsPoint(_labelWorld)) continue;
      _labelNdc.copy(_labelWorld).project(camera);
      if (_labelNdc.z < -1 || _labelNdc.z > 1) continue;
      if (_labelNdc.x < -1.05 || _labelNdc.x > 1.05) continue;
      if (_labelNdc.y < -1.05 || _labelNdc.y > 1.05) continue;
      inView.push({
        c: c,
        nx: _labelNdc.x,
        ny: _labelNdc.y
      });
    }

    inView.sort(function (a, b) {
      return a.nx * a.nx + a.ny * a.ny - (b.nx * b.nx + b.ny * b.ny);
    });

    const picked = [];
    const minD2 = CONTOUR_LABEL_MIN_NDC_DIST * CONTOUR_LABEL_MIN_NDC_DIST;
    for (let i = 0; i < inView.length && picked.length < CONTOUR_LABEL_MAX_VISIBLE; i++) {
      const v = inView[i];
      let ok = true;
      for (let j = 0; j < picked.length; j++) {
        const p = picked[j];
        const dx = v.nx - p.nx;
        const dy = v.ny - p.ny;
        if (dx * dx + dy * dy < minD2) {
          ok = false;
          break;
        }
      }
      if (ok) picked.push(v);
    }

    if (!contourLabelLayer) {
      contourLabelLayer = new THREE.Group();
      contourLabelLayer.name = 'contour-labels';
      scene.add(contourLabelLayer);
    }

    while (contourLabelSprites.length < picked.length) {
      const spr = makeElevLabelSprite('0');
      spr.visible = false;
      contourLabelLayer.add(spr);
      contourLabelSprites.push(spr);
    }

    for (let i = 0; i < contourLabelSprites.length; i++) {
      const spr = contourLabelSprites[i];
      if (i >= picked.length) {
        spr.visible = false;
        continue;
      }
      const item = picked[i];
      setLabelSpriteText(spr, formatContourLabel(item.c.level));
      spr.position.set(item.c.x, item.c.y, item.c.z);
      // Cỡ chữ theo khoảng cách; giữ nhỏ vừa để bám đường contour
      const dist = camera.position.distanceTo(spr.position);
      const s = Math.max(1.8, Math.min(5.5, dist * 0.028));
      spr.scale.set(s, s * 0.38, 1);
      spr.visible = true;
      spr.renderOrder = 999;
    }

    labelsNeedUpdate = false;
  }

  function buildContourGroup(terrain) {
    if (!terrain || !terrain.is_dem) return null;

    const fullW = terrain.width;
    const fullH = terrain.height;
    const elevFull = decodeElevations(terrain.elevations_b64, fullW, fullH);
    const maskFull = decodeMask(terrain.mask_b64, fullW, fullH);
    const zmin = terrain.zmin;
    const zmax = terrain.zmax;
    const sizes = demWorldSize(terrain);
    const worldW = sizes.worldW;
    const worldH = sizes.worldH;
    const elevScale = elevationScaleForTerrain(terrain, worldW);

    const info = contourLevels(zmin, zmax);
    if (!info.levels.length) return null;

    const grid = downsampleContourGrid(
      elevFull, maskFull, fullW, fullH, CONTOUR_GRID_MAX
    );
    const elev = grid.elev;
    const mask = grid.mask;
    const width = grid.width;
    const height = grid.height;

    const group = new THREE.Group();
    group.name = 'contours';

    const solidPositions = [];
    const dashedPositions = [];
    const labelCandidates = [];
    /** Mật hơn để khi zoom gần vẫn còn ứng viên nhãn trong vùng. */
    const labelTrack = Object.create(null);
    const LABEL_SPACING = 8;

    const levels = info.levels;
    for (let li = 0; li < levels.length; li++) {
      const level = levels[li];
      const isSolid = isContourLabelLevel(level, info.solidStep);
      const segs = findContourSegments(elev, width, height, level, mask);
      if (!segs.length) continue;

      for (let i = 0; i + 3 < segs.length; i += 4) {
        const a = contourRasterToWorld(
          segs[i], segs[i + 1], width, height, fullW, fullH,
          worldW, worldH, zmin, elevScale, level,
          elevFull, maskFull
        );
        const b = contourRasterToWorld(
          segs[i + 2], segs[i + 3], width, height, fullW, fullH,
          worldW, worldH, zmin, elevScale, level,
          elevFull, maskFull
        );
        if (isSolid) {
          solidPositions.push(a.x, a.y, a.z, b.x, b.y, b.z);
          const segLen = Math.hypot(b.x - a.x, b.z - a.z);
          let track = labelTrack[level];
          if (!track) {
            track = labelTrack[level] = { pathLen: 0, lastAt: -1e9 };
          }
          track.pathLen += segLen;
          if (track.pathLen - track.lastAt >= LABEL_SPACING) {
            labelCandidates.push({
              level: level,
              x: (a.x + b.x) * 0.5,
              y: (a.y + b.y) * 0.5 + 0.04,
              z: (a.z + b.z) * 0.5
            });
            track.lastAt = track.pathLen;
          }
        } else {
          appendDashedWorldSegment(
            dashedPositions,
            a.x, a.y, a.z, b.x, b.y, b.z,
            CONTOUR_DASH_SIZE, CONTOUR_GAP_SIZE
          );
        }
      }
    }

    if (!solidPositions.length && !dashedPositions.length) return null;

    if (solidPositions.length) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute(
        'position',
        new THREE.Float32BufferAttribute(new Float32Array(solidPositions), 3)
      );
      const mat = new THREE.LineBasicMaterial({
        color: 0x111111,
        linewidth: CONTOUR_LINE_WIDTH,
        depthTest: true,
        depthWrite: false,
        polygonOffset: true,
        polygonOffsetFactor: -2,
        polygonOffsetUnits: -2
      });
      const lines = new THREE.LineSegments(geo, mat);
      lines.renderOrder = 10;
      group.add(lines);
    }

    if (dashedPositions.length) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute(
        'position',
        new THREE.Float32BufferAttribute(new Float32Array(dashedPositions), 3)
      );
      const mat = new THREE.LineBasicMaterial({
        color: 0xe6c200,
        linewidth: CONTOUR_DASH_LINE_WIDTH,
        transparent: true,
        opacity: 0.9,
        depthTest: true,
        depthWrite: false,
        polygonOffset: true,
        polygonOffsetFactor: -2,
        polygonOffsetUnits: -2
      });
      const lines = new THREE.LineSegments(geo, mat);
      lines.renderOrder = 10;
      group.add(lines);
    }

    // Nhãn không gắn sẵn — cập nhật theo viewport khi zoom/pan
    contourLabelData = labelCandidates;
    labelsNeedUpdate = true;

    return group;
  }

  function syncContours() {
    disposeObject3D(contourGroup);
    contourGroup = null;
    clearContourLabels();
    if (!contoursVisible || !lastTerrain || !scene) return;
    contourGroup = buildContourGroup(lastTerrain);
    if (contourGroup) scene.add(contourGroup);
    updateContourLabelsInView();
  }

  function setContoursVisible(visible) {
    contoursVisible = !!visible;
    syncContours();
  }

  async function rebuildFromLast() {
    if (!lastTerrain || !scene) return;
    disposeObject3D(currentMesh);
    currentMesh = await buildTerrainMesh(lastTerrain);
    scene.add(currentMesh);
    syncContours();
    if (global.Flow3D && typeof global.Flow3D.redrawOverlay === 'function') {
      global.Flow3D.redrawOverlay();
    }
    if (global.FlowRun && typeof global.FlowRun.redrawOverlay === 'function') {
      global.FlowRun.redrawOverlay();
    }
    if (global.Flood3D && typeof global.Flood3D.redrawOverlay === 'function') {
      global.Flood3D.redrawOverlay();
    }
  }

  async function loadDem3D(fileId, maxSize) {
    maxSize = maxSize || 256;
    if (!fileId) {
      alert('Hãy mở ít nhất một GeoTIFF.');
      return;
    }

    const exagInput = $('demExaggeration');
    if (exagInput) {
      exaggeration = Number(exagInput.value) || 0.05;
    }

    initViewer();

    const res = await fetch('/api/geotiff/dem/file/' + fileId + '/terrain?max_size=' + maxSize);
    const data = await res.json();
    if (!data.success) {
      throw new Error(data.message || 'Không tải được terrain DEM');
    }

    lastTerrain = data;
    lastPoints = null;

    disposeObject3D(currentMesh);
    currentMesh = await buildTerrainMesh(data);
    scene.add(currentMesh);

    // Không tự bật contour khi load terrain
    syncContours();

    fitCameraToObjects(currentMesh, null);
    updateElevationLegend(data);

    lastOsmKey = '';
    refreshOsmBasemap(data)
      .then(function () {
        if (!scene || lastTerrain !== data) return;
        if (osmMesh) fitCameraToObjects(currentMesh, osmMesh);
      })
      .catch(function (e) {
        console.warn('Không tải được nền basemap:', e);
      });

    if (global.Flow3D && typeof global.Flow3D.onTerrainReady === 'function') {
      global.Flow3D.onTerrainReady();
    }
    if (global.FlowRun && typeof global.FlowRun.onTerrainReady === 'function') {
      global.FlowRun.onTerrainReady();
    }
    if (global.Flood3D && typeof global.Flood3D.onTerrainReady === 'function') {
      global.Flood3D.onTerrainReady();
    }
  }

  function pointsTerrainFromWgs84(data) {
    const b = data.bounds_wgs84 || {};
    const west = Number(b.west);
    const south = Number(b.south);
    const east = Number(b.east);
    const north = Number(b.north);
    const zs = data.z || [];
    let zmin = Number(data.zmin);
    let zmax = Number(data.zmax);
    if (!Number.isFinite(zmin) || !Number.isFinite(zmax)) {
      zmin = Infinity;
      zmax = -Infinity;
      for (let i = 0; i < zs.length; i++) {
        const z = Number(zs[i]);
        if (!Number.isFinite(z)) continue;
        if (z < zmin) zmin = z;
        if (z > zmax) zmax = z;
      }
      if (!Number.isFinite(zmin)) {
        zmin = 0;
        zmax = 1;
      }
    }
    return {
      crs: 'local-en',
      is_dem: true,
      width: 2,
      height: 2,
      zmin: zmin,
      zmax: zmax,
      bounds_wgs84: { west: west, south: south, east: east, north: north },
      bounds: { left: west, bottom: south, right: east, top: north }
    };
  }

  function parseGeoFrame(raw) {
    if (!raw) return null;
    const x0 = Number(raw.x0);
    const y0 = Number(raw.y0);
    const lon0 = Number(raw.lon0);
    const lat0 = Number(raw.lat0);
    const mx = Number(raw.meters_per_lon);
    const my = Number(raw.meters_per_lat);
    if (![x0, y0, lon0, lat0, mx, my].every(Number.isFinite) || mx < 1 || my < 1) return null;
    return { x0: x0, y0: y0, lon0: lon0, lat0: lat0, meters_per_lon: mx, meters_per_lat: my };
  }

  function localXyToLonLat(x, y, frame) {
    return {
      lon: frame.lon0 + (x - frame.x0) / frame.meters_per_lon,
      lat: frame.lat0 + (y - frame.y0) / frame.meters_per_lat
    };
  }

  function parseLasBinary(buffer) {
    if (!buffer || buffer.byteLength < 96) {
      throw new Error('May diem LAS binary qua ngan');
    }
    const view = new DataView(buffer);
    const magic = String.fromCharCode(
      view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3)
    );
    if (magic !== 'DTP1' && magic !== 'DTP2') {
      throw new Error('Sai dinh dang may diem LAS');
    }
    const packed = magic === 'DTP2';
    const n = view.getUint32(4, true);
    const flags = view.getUint32(8, true);
    const hasRgb = (flags & 1) !== 0;
    let o = 16;
    function f64() {
      const v = view.getFloat64(o, true);
      o += 8;
      return v;
    }
    const xmin = f64();
    const xmax = f64();
    const ymin = f64();
    const ymax = f64();
    const zmin = f64();
    const zmax = f64();
    const west = f64();
    const south = f64();
    const east = f64();
    const north = f64();
    const xyzBytes = packed ? n * 6 : n * 12;
    const rgbBytes = hasRgb ? n * 3 : 0;
    if (buffer.byteLength < 96 + xyzBytes + rgbBytes) {
      throw new Error('May diem LAS binary thieu du lieu');
    }
    let localXyz;
    if (packed) {
      const q = new Uint16Array(buffer, 96, n * 3);
      localXyz = new Float32Array(n * 3);
      const sx = (xmax - xmin) / 65535;
      const sy = (ymax - ymin) / 65535;
      const sz = (zmax - zmin) / 65535;
      for (let i = 0, i3 = 0; i < n; i++, i3 += 3) {
        localXyz[i3] = xmin + q[i3] * sx;
        localXyz[i3 + 1] = ymin + q[i3 + 1] * sy;
        localXyz[i3 + 2] = zmin + q[i3 + 2] * sz;
      }
    } else {
      localXyz = new Float32Array(new Float32Array(buffer, 96, n * 3));
    }
    return {
      localXyz: localXyz,
      rgb: hasRgb ? new Uint8Array(buffer, 96 + xyzBytes, n * 3) : null,
      xmin: xmin,
      xmax: xmax,
      ymin: ymin,
      ymax: ymax,
      zmin: zmin,
      zmax: zmax,
      shown: n,
      total: n,
      bounds_wgs84: { west: west, south: south, east: east, north: north }
    };
  }

  function strideReorderChunk(pos, col, zRaw, n) {
    if (n < 16) return;
    const step = Math.max(2, Math.floor(Math.sqrt(n)));
    const p2 = pos.slice();
    const c2 = col.slice();
    const z2 = zRaw ? zRaw.slice() : null;
    let w = 0;
    for (let s = 0; s < step; s++) {
      for (let i = s; i < n; i += step) {
        const so = i * 3;
        const d = w * 3;
        pos[d] = p2[so];
        pos[d + 1] = p2[so + 1];
        pos[d + 2] = p2[so + 2];
        col[d] = c2[so];
        col[d + 1] = c2[so + 1];
        col[d + 2] = c2[so + 2];
        if (z2) zRaw[w] = z2[i];
        w += 1;
      }
    }
  }

  function buildPointCloud(data, terrain) {
    const xyz = data.localXyz;
    const lons = data.lon || data.x || [];
    const lats = data.lat || data.y || [];
    const zs = data.z || [];
    const n = xyz ? ((data.shown || xyz.length / 3) | 0) : Math.min(lons.length, lats.length, zs.length);
    const sizes = demWorldSize(terrain);
    const worldW = sizes.worldW;
    const worldH = sizes.worldH;
    const elevScale = elevationScaleForTerrain(terrain, worldW);
    const zmin = terrain.zmin;
    const zrange = Math.max(terrain.zmax - zmin, 1e-6);
    const rgbFlat = data.rgb;
    const rgbNested = data.colors;
    const rgbIsByte = rgbFlat instanceof Uint8Array;
    const rgbScale = rgbIsByte ? 1 : (rgbFlat && rgbFlat.length && Number(rgbFlat[0]) > 1 ? 1 / 255 : 1);

    function writeColor(i, zVal, dest, d) {
      const i3 = i * 3;
      if (rgbFlat && rgbFlat.length >= i3 + 3) {
        if (rgbIsByte) {
          dest[d] = rgbFlat[i3];
          dest[d + 1] = rgbFlat[i3 + 1];
          dest[d + 2] = rgbFlat[i3 + 2];
        } else {
          dest[d] = Math.max(0, Math.min(255, Math.round(Number(rgbFlat[i3]) * rgbScale * 255)));
          dest[d + 1] = Math.max(0, Math.min(255, Math.round(Number(rgbFlat[i3 + 1]) * rgbScale * 255)));
          dest[d + 2] = Math.max(0, Math.min(255, Math.round(Number(rgbFlat[i3 + 2]) * rgbScale * 255)));
        }
        return;
      }
      if (rgbNested && rgbNested[i] && rgbNested[i].length >= 3) {
        const scale = Number(rgbNested[i][0]) > 1 ? 1 : 255;
        dest[d] = Math.max(0, Math.min(255, Math.round(Number(rgbNested[i][0]) * scale)));
        dest[d + 1] = Math.max(0, Math.min(255, Math.round(Number(rgbNested[i][1]) * scale)));
        dest[d + 2] = Math.max(0, Math.min(255, Math.round(Number(rgbNested[i][2]) * scale)));
        return;
      }
      const col = heightColorRgb((zVal - zmin) / zrange);
      dest[d] = Math.round(col.r * 255);
      dest[d + 1] = Math.round(col.g * 255);
      dest[d + 2] = Math.round(col.b * 255);
    }

    const useChunks = n > POINT_INTERACT_MAX && !!xyz;
    const gx = useChunks ? POINT_CHUNK_AXIS : 1;
    const gz = useChunks ? POINT_CHUNK_AXIS : 1;
    const cellN = gx * gz;
    const counts = new Uint32Array(cellN);
    const geoFrame = parseGeoFrame(data.frame);
    const xmin = Number(data.xmin);
    const ymin = Number(data.ymin);
    const xspan = Math.max(Number(data.xmax) - xmin, 1e-9);
    const yspan = Math.max(Number(data.ymax) - ymin, 1e-9);

    function cellIndex(i) {
      if (!xyz || cellN === 1) return 0;
      const i3 = i * 3;
      let ix = Math.floor(((xyz[i3] - xmin) / xspan) * gx);
      let iz = Math.floor(((xyz[i3 + 1] - ymin) / yspan) * gz);
      if (ix < 0) ix = 0;
      else if (ix >= gx) ix = gx - 1;
      if (iz < 0) iz = 0;
      else if (iz >= gz) iz = gz - 1;
      return iz * gx + ix;
    }

    if (xyz) {
      for (let i = 0; i < n; i++) counts[cellIndex(i)]++;
    } else {
      counts[0] = n;
    }

    const cells = new Array(cellN);
    for (let c = 0; c < cellN; c++) {
      if (!counts[c]) continue;
      cells[c] = {
        pos: new Float32Array(counts[c] * 3),
        col: new Uint8Array(counts[c] * 3),
        z: new Float32Array(counts[c]),
        filled: 0
      };
    }

    if (xyz && n > 0) {
      for (let i = 0; i < n; i++) {
        const i3 = i * 3;
        const cell = cells[cellIndex(i)];
        const d = cell.filled * 3;
        if (geoFrame) {
          const ll = localXyToLonLat(xyz[i3], xyz[i3 + 1], geoFrame);
          const loc = lonLatToWorld(ll.lon, ll.lat, terrain, worldW, worldH);
          cell.pos[d] = loc.x;
          cell.pos[d + 2] = loc.z;
        } else {
          const u = (xyz[i3] - xmin) / xspan;
          const v = (xyz[i3 + 1] - ymin) / yspan;
          cell.pos[d] = (u - 0.5) * worldW;
          cell.pos[d + 2] = -(v - 0.5) * worldH;
        }
        cell.pos[d + 1] = (xyz[i3 + 2] - zmin) * elevScale;
        writeColor(i, xyz[i3 + 2], cell.col, d);
        cell.z[cell.filled] = xyz[i3 + 2];
        cell.filled += 1;
      }
    } else {
      const cell = cells[0];
      for (let i = 0; i < n; i++) {
        const loc = lonLatToWorld(Number(lons[i]), Number(lats[i]), terrain, worldW, worldH);
        const d = i * 3;
        cell.pos[d] = loc.x;
        cell.pos[d + 1] = (Number(zs[i]) - zmin) * elevScale;
        cell.pos[d + 2] = loc.z;
        writeColor(i, Number(zs[i]), cell.col, d);
        cell.z[i] = Number(zs[i]);
      }
      if (cell) cell.filled = n;
    }

    const group = new THREE.Group();
    group.name = 'pointCloud';
    group.userData.totalPoints = n;
    group.userData.isPointCloud = true;
    const yMax = Math.max((terrain.zmax - zmin) * elevScale, 0.01);
    const pointSize = n > 1000000 ? 1.3 : n > 80000 ? 2 : 2.6;

    for (let cz = 0; cz < gz; cz++) {
      for (let cx = 0; cx < gx; cx++) {
        const cell = cells[cz * gx + cx];
        if (!cell || !cell.filled) continue;
        strideReorderChunk(cell.pos, cell.col, cell.z, cell.filled);
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute('position', new THREE.BufferAttribute(cell.pos, 3));
        geometry.setAttribute('color', new THREE.BufferAttribute(cell.col, 3, true));
        geometry.computeBoundingBox();
        geometry.computeBoundingSphere();
        if (geometry.boundingBox) {
          geometry.boundingBox.expandByScalar(Math.max(worldW / gx, worldH / gz, yMax) * 0.12);
        }
        if (geometry.boundingSphere) {
          geometry.boundingSphere.radius *= 1.2;
        }
        const points = new THREE.Points(geometry, new THREE.PointsMaterial({
          size: pointSize,
          vertexColors: true,
          sizeAttenuation: false
        }));
        points.frustumCulled = false;
        points.userData.zRaw = cell.z;
        points.userData.chunkCount = cell.filled;
        group.add(points);
      }
    }
    return group;
  }

  async function loadPointsWgs84(data, opts) {
    opts = opts || {};
    if (!data || !(data.lon || data.x || data.localXyz)) {
      throw new Error('Khong co diem WGS84');
    }
    const exagInput = $('demExaggeration');
    if (exagInput) {
      exaggeration = Number(exagInput.value) || 0.05;
    }
    const replace = !!(opts.replace && currentMesh && scene);
    if (!replace && !opts.skipInit) {
      initViewer({ performance: !!(data.shown > POINT_INTERACT_MAX || (data.localXyz && data.localXyz.length > POINT_INTERACT_MAX * 3)) });
    }
    lastPoints = data;
    if (!replace || !lastTerrain) lastTerrain = pointsTerrainFromWgs84(data);
    if (data.frame) lastTerrain.frame = data.frame;
    pointLodMode = '';
    disposeObject3D(previewMesh);
    previewMesh = null;
    const old = currentMesh;
    currentMesh = buildPointCloud(data, lastTerrain);
    scene.add(currentMesh);
    if (old) disposeObject3D(old);
    ensurePreviewCloud();
    if (!replace && !opts.skipInit) fitCameraToObjects(currentMesh, null);
    settlePointLod();
    applyStoredPointAppearance();
    requestRender();
    if (!replace) {
      lastOsmKey = '';
      refreshOsmBasemap(lastTerrain)
        .then(function () {
          if (!scene || !lastPoints) return;
          if (osmMesh && !opts.skipInit) fitCameraToObjects(currentMesh, osmMesh);
        })
        .catch(function (e) {
          console.warn('Không tải được nền basemap:', e);
        });
    }
  }

  function fitCameraToObjects(demObj, osmObj) {
    if (!controls || !camera) return;
    const focus = osmObj || demObj;
    if (!focus) return;
    const box = new THREE.Box3().setFromObject(focus);
    if (demObj && osmObj) box.expandByObject(demObj);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    controls.target.copy(center);
    const fitDist = Math.max(size.x, size.z, size.y, 1) * 1.15;
    pointFitDist = fitDist;
    camera.position.set(
      center.x + fitDist * 0.85,
      center.y + fitDist * 0.55,
      center.z + fitDist * 0.85
    );
    controls.update();
    // Zoom out tối đa ~mức 4; zoom in tới DEM3D_MAX_ZOOM (32)
    controls.maxDistance = fitDist * Math.pow(2, DEM3D_FIT_ZOOM - 4);
    controls.minDistance = Math.max(
      0.02,
      fitDist * Math.pow(2, DEM3D_FIT_ZOOM - DEM3D_MAX_ZOOM)
    );
    camera.near = Math.max(0.005, controls.minDistance * 0.05);
    camera.far = Math.max(100000, controls.maxDistance * 20);
    camera.updateProjectionMatrix();
    requestRender();
  }

  function rescalePointCloudHeights() {
    if (!lastPoints || !currentMesh || !lastTerrain) return;
    const sizes = demWorldSize(lastTerrain);
    const elevScale = elevationScaleForTerrain(lastTerrain, sizes.worldW);
    const zmin = lastTerrain.zmin;
    const yMax = Math.max((lastTerrain.zmax - zmin) * elevScale, 0.01);
    const children = currentMesh.isGroup ? currentMesh.children : [currentMesh];
    for (let c = 0; c < children.length; c++) {
      const child = children[c];
      const attr = child.geometry && child.geometry.getAttribute('position');
      const zRaw = child.userData && child.userData.zRaw;
      if (!attr || !attr.array || !zRaw) continue;
      const arr = attr.array;
      for (let i = 0; i < zRaw.length; i++) {
        arr[i * 3 + 1] = (zRaw[i] - zmin) * elevScale;
      }
      attr.needsUpdate = true;
      if (child.geometry.boundingBox) {
        child.geometry.boundingBox.min.y = 0;
        child.geometry.boundingBox.max.y = yMax;
        if (child.geometry.boundingSphere) {
          child.geometry.boundingBox.getBoundingSphere(child.geometry.boundingSphere);
        }
      }
    }
    const prevMode = pointLodMode;
    pointLodMode = '';
    ensurePreviewCloud();
    setPointCloudLod(prevMode === 'preview' ? 'preview' : 'full');
    requestRender();
  }

  function hexToRgb(hex) {
    const t = String(hex || '').replace('#', '');
    const n = parseInt(t.length === 3 ? t[0]+t[0]+t[1]+t[1]+t[2]+t[2] : t, 16);
    if (!Number.isFinite(n)) return { r: 56, g: 189, b: 248 };
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }

  function forEachPointCloud(fn) {
    const roots = [];
    if (currentMesh && (currentMesh.userData.isPointCloud || (currentMesh.isGroup && currentMesh.name === 'pointCloud'))) {
      roots.push(currentMesh);
    }
    if (previewMesh) roots.push(previewMesh);
    roots.forEach(function (root) {
      const list = root.isPoints ? [root] : (root.children || []);
      list.forEach(fn);
    });
  }

  function applyStoredPointAppearance() {
    setPointAppearance(lastPointAppearance, true);
  }

  function setPointAppearance(style, keep) {
    style = style || {};
    const size = Math.max(0.5, Number(style.size) || lastPointAppearance.size || 4);
    const colorMode = style.colorMode || lastPointAppearance.colorMode || 'rgb';
    const color = style.color || lastPointAppearance.color || '#38bdf8';
    if (!keep) lastPointAppearance = { size: size, colorMode: colorMode, color: color };
    if (!currentMesh || !global.THREE) return;
    const rgb = hexToRgb(color);
    forEachPointCloud(function (obj) {
      if (!obj || !obj.isPoints) return;
      const mat = obj.material;
      const geom = obj.geometry;
      if (mat) {
        mat.size = size;
        mat.sizeAttenuation = false;
        mat.needsUpdate = true;
      }
      if (!geom) return;
      const colAttr = geom.getAttribute('color');
      if (colAttr && !geom.userData.rgbOrig) {
        geom.userData.rgbOrig = new Uint8Array(colAttr.array);
      }
      if (colorMode === 'solid') {
        if (mat) {
          mat.vertexColors = false;
          mat.color.setRGB(rgb.r / 255, rgb.g / 255, rgb.b / 255);
        }
        return;
      }
      if (mat) {
        mat.vertexColors = true;
        mat.color.setRGB(1, 1, 1);
      }
      if (!colAttr) return;
      const dest = colAttr.array;
      if (colorMode === 'rgb' && geom.userData.rgbOrig) {
        dest.set(geom.userData.rgbOrig);
        colAttr.needsUpdate = true;
        return;
      }
      if (colorMode !== 'height') return;
      const zRaw = obj.userData && obj.userData.zRaw;
      const n = colAttr.count;
      let zmin = Infinity;
      let zmax = -Infinity;
      if (zRaw && zRaw.length) {
        for (let i = 0; i < zRaw.length; i++) {
          const z = zRaw[i];
          if (z < zmin) zmin = z;
          if (z > zmax) zmax = z;
        }
      }
      const zspan = Math.max(zmax - zmin, 1e-6);
      for (let i = 0; i < n; i++) {
        let t = 0.5;
        if (zRaw && i < zRaw.length) t = (zRaw[i] - zmin) / zspan;
        const c = heightColorRgb(t);
        dest[i * 3] = Math.round(c.r * 255);
        dest[i * 3 + 1] = Math.round(c.g * 255);
        dest[i * 3 + 2] = Math.round(c.b * 255);
      }
      colAttr.needsUpdate = true;
    });
    requestRender();
  }

  async function setExaggeration(value) {
    exaggeration = Number(value) || 0.05;
    if (currentMesh && currentMesh.userData && currentMesh.userData.isObjMesh) {
      rescaleObjMeshHeights();
      return;
    }
    if (lastPoints && lastPoints.localXyz && lastPoints.shown > 400000) {
      if (pointExagTimer) cancelAnimationFrame(pointExagTimer);
      pointExagTimer = requestAnimationFrame(function () {
        pointExagTimer = null;
        rescalePointCloudHeights();
      });
      return;
    }
    if (lastPoints) {
      await loadPointsWgs84(lastPoints, { skipInit: true });
      return;
    }
    if (lastTerrain) {
      await rebuildFromLast();
    }
  }

  function formatElev(v) {
    if (!Number.isFinite(v)) return '—';
    const abs = Math.abs(v);
    if (abs >= 1000) return v.toFixed(0);
    if (abs >= 10) return v.toFixed(1);
    return v.toFixed(2);
  }

  function updateElevationLegend(terrain) {
    const legend = $('dem3dLegend');
    if (!legend) return;
    if (!terrain || !terrain.is_dem) {
      legend.hidden = true;
      return;
    }
    const zmin = terrain.zmin;
    const zmax = terrain.zmax;
    const zmid = (zmin + zmax) / 2;
    const elMax = $('dem3dLegendMax');
    const elMid = $('dem3dLegendMid');
    const elMin = $('dem3dLegendMin');
    if (elMax) elMax.textContent = formatElev(zmax);
    if (elMid) elMid.textContent = formatElev(zmid);
    if (elMin) elMin.textContent = formatElev(zmin);
    legend.hidden = false;
  }

  function typedArrayView(buffer, Type, byteOffset, count) {
    const bytes = count * Type.BYTES_PER_ELEMENT;
    if ((byteOffset % Type.BYTES_PER_ELEMENT) === 0) {
      return new Type(buffer, byteOffset, count);
    }
    return new Type(buffer.slice(byteOffset, byteOffset + bytes));
  }

  function parseObjMeshBinary(buffer) {
    const dv = new DataView(buffer);
    const mag = String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3));
    if (mag !== 'DTM1' && mag !== 'DTM2') {
      throw new Error('Mesh binary khong hop le');
    }
    const nverts = dv.getUint32(4, true);
    const nidx = dv.getUint32(8, true);
    const flags = dv.getUint32(12, true);
    const xmin = dv.getFloat32(16, true);
    const xmax = dv.getFloat32(20, true);
    const ymin = dv.getFloat32(24, true);
    const ymax = dv.getFloat32(28, true);
    const zmin = dv.getFloat32(32, true);
    const zmax = dv.getFloat32(36, true);
    let off = 40;
    const positions = typedArrayView(buffer, Float32Array, off, nverts * 3);
    off += nverts * 12;
    let colors = null;
    if (flags & 1) {
      colors = typedArrayView(buffer, Uint8Array, off, nverts * 3);
      off += nverts * 3;
      off = (off + 3) & ~3;
    }
    let uvs = null;
    if (flags & 2) {
      uvs = typedArrayView(buffer, Float32Array, off, nverts * 2);
      off += nverts * 8;
    }
    let normals = null;
    if (flags & 4) {
      normals = typedArrayView(buffer, Float32Array, off, nverts * 3);
      off += nverts * 12;
    }
    let pages = null;
    if (flags & 8) {
      pages = typedArrayView(buffer, Uint8Array, off, nverts);
      off += nverts;
      off = (off + 3) & ~3;
    }
    let indices = null;
    if (nidx > 0) {
      indices = typedArrayView(buffer, Uint32Array, off, nidx);
    }
    return {
      positions: positions,
      colors: colors,
      uvs: uvs,
      normals: normals,
      pages: pages,
      indices: indices,
      vertices: nverts,
      faces: nidx > 0 ? Math.floor(nidx / 3) : Math.floor(nverts / 3),
      xmin: xmin,
      xmax: xmax,
      ymin: ymin,
      ymax: ymax,
      zmin: zmin,
      zmax: zmax
    };
  }

  function fitCameraToPositions(positions, box) {
    if (!camera || !controls) return;
    let xmin, xmax, ymin, ymax, zmin, zmax;
    if (box && Number.isFinite(box.xmin) && Number.isFinite(box.xmax)) {
      xmin = box.xmin; xmax = box.xmax;
      ymin = box.ymin; ymax = box.ymax;
      zmin = box.zmin; zmax = box.zmax;
    } else {
      xmin = Infinity; ymin = Infinity; zmin = Infinity;
      xmax = -Infinity; ymax = -Infinity; zmax = -Infinity;
      const arr = positions || [];
      for (let i = 0; i < arr.length; i += 3) {
        const x = arr[i], y = arr[i + 1], z = arr[i + 2];
        if (x < xmin) xmin = x;
        if (x > xmax) xmax = x;
        if (y < ymin) ymin = y;
        if (y > ymax) ymax = y;
        if (z < zmin) zmin = z;
        if (z > zmax) zmax = z;
      }
    }
    const cx = (xmin + xmax) * 0.5;
    const cy = (ymin + ymax) * 0.5;
    const cz = (zmin + zmax) * 0.5;
    const radius = Math.max(xmax - xmin, ymax - ymin, zmax - zmin, 1);
    controls.target.set(cx, cy, cz);
    camera.near = Math.max(radius / 4000, 0.05);
    camera.far = radius * 40;
    camera.position.set(cx + radius * 0.75, cy + radius * 0.55, cz + radius * 0.85);
    camera.updateProjectionMatrix();
    controls.update();
    requestRender();
  }

  function placeObjMeshChunk(positions, normals, data, terrain, zRaw, start, end) {
    const sizes = demWorldSize(terrain);
    const worldW = sizes.worldW;
    const worldH = sizes.worldH;
    const elevScale = elevationScaleForTerrain(terrain, worldW);
    const zmin = terrain.zmin;
    const geoFrame = parseGeoFrame(data.frame || terrain.frame);
    const objXmin = Number(data.xmin);
    const objXmax = Number(data.xmax);
    const objYmin = -Number(data.zmax);
    const objYmax = -Number(data.zmin);
    const xspan = Math.max(objXmax - objXmin, 1e-9);
    const yspan = Math.max(objYmax - objYmin, 1e-9);
    const sx = worldW / xspan;
    const sy = elevScale;
    const sz = worldH / yspan;
    for (let i = start; i < end; i++) {
      const i3 = i * 3;
      const objX = positions[i3];
      const elev = positions[i3 + 1];
      const objY = -positions[i3 + 2];
      zRaw[i] = elev;
      if (geoFrame) {
        const ll = localXyToLonLat(objX, objY, geoFrame);
        const loc = lonLatToWorld(ll.lon, ll.lat, terrain, worldW, worldH);
        positions[i3] = loc.x;
        positions[i3 + 1] = (elev - zmin) * elevScale;
        positions[i3 + 2] = loc.z;
      } else {
        positions[i3] = ((objX - objXmin) / xspan - 0.5) * worldW;
        positions[i3 + 1] = (elev - zmin) * elevScale;
        positions[i3 + 2] = -((objY - objYmin) / yspan - 0.5) * worldH;
      }
      if (normals) {
        const nx = normals[i3] * sx;
        const ny = normals[i3 + 1] * sy;
        const nz = normals[i3 + 2] * sz;
        const len = Math.sqrt(nx * nx + ny * ny + nz * nz) || 1;
        normals[i3] = nx / len;
        normals[i3 + 1] = ny / len;
        normals[i3 + 2] = nz / len;
      }
    }
  }

  function placeObjMeshInOsmWorld(positions, normals, data, terrain) {
    const n = (positions.length / 3) | 0;
    const zRaw = new Float32Array(n);
    placeObjMeshChunk(positions, normals, data, terrain, zRaw, 0, n);
    return zRaw;
  }

  async function placeObjMeshInOsmWorldAsync(positions, normals, data, terrain, onProgress) {
    const n = (positions.length / 3) | 0;
    const zRaw = new Float32Array(n);
    const chunk = 180000;
    for (let i = 0; i < n; i += chunk) {
      placeObjMeshChunk(positions, normals, data, terrain, zRaw, i, Math.min(n, i + chunk));
      if (onProgress && n) onProgress(Math.min(1, (i + chunk) / n));
      if (i + chunk < n) {
        await new Promise(function (resolve) { setTimeout(resolve, 0); });
      }
    }
    return zRaw;
  }

  function rescaleObjMeshHeights() {
    if (!currentMesh || !currentMesh.userData || !currentMesh.userData.zRaw || !lastTerrain) return;
    const zRaw = currentMesh.userData.zRaw;
    const attr = currentMesh.geometry && currentMesh.geometry.getAttribute('position');
    if (!attr || !attr.array) return;
    const sizes = demWorldSize(lastTerrain);
    const elevScale = elevationScaleForTerrain(lastTerrain, sizes.worldW);
    const zmin = lastTerrain.zmin;
    const arr = attr.array;
    for (let i = 0; i < zRaw.length; i++) {
      arr[i * 3 + 1] = (zRaw[i] - zmin) * elevScale;
    }
    attr.needsUpdate = true;
    requestRender();
  }

  async function loadObjMesh(buffer, textureUrl, geo, opts) {
    geo = geo || {};
    opts = opts || {};
    const wgs = geo.bounds_wgs84 || null;
    const hasOsm = !!(wgs && Number.isFinite(Number(wgs.west)) && Number.isFinite(Number(wgs.east)));
    const replace = !!(opts.replace && currentMesh && scene);
    const exagInput = $('demExaggeration');
    if (exagInput) {
      exaggeration = Number(exagInput.value) || (hasOsm ? 0.05 : 1);
    } else if (hasOsm) {
      exaggeration = 0.05;
    }

    if (!replace) {
      initViewer({
        freeOrbit: !hasOsm,
        background: hasOsm ? 0x87a8c4 : 0x1b2838,
        performance: true,
        sharp: true
      });
      lastPoints = null;
    }
    const data = parseObjMeshBinary(buffer);
    if (geo.frame) data.frame = geo.frame;
    if (Number.isFinite(Number(geo.zmin))) data.ymin = Number(geo.zmin);
    if (Number.isFinite(Number(geo.zmax))) data.ymax = Number(geo.zmax);
    if (geo.local) {
      const loc = geo.local;
      if (Number.isFinite(Number(loc.xmin))) data.xmin = Number(loc.xmin);
      if (Number.isFinite(Number(loc.xmax))) data.xmax = Number(loc.xmax);
      if (Number.isFinite(Number(loc.ymin))) data.ymin = Number(loc.ymin);
      if (Number.isFinite(Number(loc.ymax))) data.ymax = Number(loc.ymax);
      if (Number.isFinite(Number(loc.zmin))) data.zmin = Number(loc.zmin);
      if (Number.isFinite(Number(loc.zmax))) data.zmax = Number(loc.zmax);
    }

    const geom = new THREE.BufferGeometry();
    geom.setAttribute('position', new THREE.BufferAttribute(data.positions, 3));
    if (data.uvs) {
      geom.setAttribute('uv', new THREE.BufferAttribute(data.uvs, 2));
    }
    if (data.colors) {
      geom.setAttribute('color', new THREE.BufferAttribute(data.colors, 3, true));
    }
    if (data.normals) {
      geom.setAttribute('normal', new THREE.BufferAttribute(data.normals, 3));
    }
    if (data.indices && data.indices.length) {
      geom.setIndex(new THREE.BufferAttribute(data.indices, 1));
    }

    let zRaw = null;
    if (hasOsm) {
      if (!replace || !lastTerrain) {
        lastTerrain = pointsTerrainFromWgs84({
          bounds_wgs84: wgs,
          zmin: data.ymin,
          zmax: data.ymax
        });
        if (geo.frame) lastTerrain.frame = geo.frame;
      }
      if (opts.asyncPlace) {
        zRaw = await placeObjMeshInOsmWorldAsync(data.positions, data.normals, data, lastTerrain, opts.onProgress);
      } else {
        zRaw = placeObjMeshInOsmWorld(data.positions, data.normals, data, lastTerrain);
      }
    } else if (!replace) {
      lastTerrain = null;
    }

    let maps = [];
    const texUrls = Array.isArray(textureUrl) ? textureUrl.filter(Boolean) : (textureUrl ? [textureUrl] : []);
    const reuseMats = replace && !opts.reloadTexture && currentMesh && currentMesh.material;
    if (reuseMats) {
      const old = Array.isArray(currentMesh.material) ? currentMesh.material : [currentMesh.material];
      maps = old.map(function (m) { return m && m.map; }).filter(Boolean);
    } else if (texUrls.length && data.uvs) {
      for (let i = 0; i < texUrls.length; i++) {
        try {
          const tex = await loadTexture(texUrls[i], { sharp: true, anisotropy: 16 });
          tex.flipY = true;
          tex.needsUpdate = true;
          maps.push(tex);
        } catch (e) {}
      }
    }
    function makeMat(map) {
      const mat = new THREE.MeshBasicMaterial({
        map: map || undefined,
        vertexColors: !map && !!data.colors,
        color: map || data.colors ? 0xffffff : 0xb8c4ce,
        side: THREE.DoubleSide
      });
      if (map) applySharpMeshMaterial(mat, map);
      return mat;
    }
    const materials = maps.length ? maps.map(makeMat) : [makeMat(null)];
    if (data.pages && data.indices && materials.length > 1) {
      const idx = data.indices;
      const buckets = [];
      for (let i = 0; i < materials.length; i++) buckets.push([]);
      for (let i = 0; i + 2 < idx.length; i += 3) {
        const p = Math.min(materials.length - 1, data.pages[idx[i]] || 0);
        buckets[p].push(idx[i], idx[i + 1], idx[i + 2]);
      }
      const merged = new Uint32Array(idx.length);
      let w = 0;
      geom.clearGroups();
      for (let p = 0; p < buckets.length; p++) {
        const b = buckets[p];
        if (!b.length) continue;
        merged.set(b, w);
        geom.addGroup(w, b.length, p);
        w += b.length;
      }
      geom.setIndex(new THREE.BufferAttribute(merged, 1));
    }
    const largeMesh = data.vertices > 400000;
    if (largeMesh) {
      geom.boundingSphere = null;
    } else {
      geom.computeBoundingSphere();
    }
    const old = currentMesh;
    currentMesh = new THREE.Mesh(geom, materials.length > 1 ? materials : materials[0]);
    currentMesh.frustumCulled = !largeMesh;
    currentMesh.userData.isObjMesh = true;
    currentMesh.userData.zRaw = zRaw;
    scene.add(currentMesh);
    if (old) disposeObject3D(old);
    if (!replace) {
      if (hasOsm) {
        fitCameraToObjects(currentMesh, null);
        lastOsmKey = '';
        refreshOsmBasemap(lastTerrain)
          .then(function () {
            if (!scene || !currentMesh || !currentMesh.userData.isObjMesh) return;
            if (osmMesh) fitCameraToObjects(currentMesh, osmMesh);
          })
          .catch(function (e) {
            console.warn('Không tải được nền OSM cho mesh OBJ:', e);
          });
      } else {
        fitCameraToPositions(data.positions, data);
      }
    }
    requestRender();
    return data;
  }

  function show3DMode(show) {
    const mapEl = $('map');
    const view3d = $('view3d');
    const demControls = $('dem3dControls');
    const legend = $('dem3dLegend');
    if (mapEl) mapEl.style.display = show ? 'none' : '';
    if (view3d) view3d.hidden = !show;
    if (demControls) demControls.hidden = !show;
    if (!show) {
      if (legend) legend.hidden = true;
      if (global.Flow3D && typeof global.Flow3D.teardown === 'function') {
        global.Flow3D.teardown();
      }
      if (global.FlowRun && typeof global.FlowRun.teardown === 'function') {
        global.FlowRun.teardown();
      }
      if (global.Flood3D && typeof global.Flood3D.teardown === 'function') {
        global.Flood3D.teardown();
      }
      destroyViewer();
      if (typeof global.resizeLeafletMap === 'function') {
        setTimeout(function () { global.resizeLeafletMap(); }, 40);
      }
    }
  }

  function webMercatorToLonLat(x, y) {
    const lon = (x / WEB_MERCATOR_MAX) * 180;
    const lat = (Math.atan(Math.sinh(y / WEB_MERCATOR_MAX * Math.PI)) * 180) / Math.PI;
    return { lon: lon, lat: lat };
  }

  function worldToLonLat(wx, wz, terrain) {
    const sizes = demWorldSize(terrain);
    const worldW = sizes.worldW;
    const worldH = sizes.worldH;
    const native = parseNativeBounds(terrain && terrain.bounds);
    const wgs = parseDemBounds(terrain && terrain.bounds_wgs84);

    function fromProj(px, py, proj, mercator) {
      if (mercator) return webMercatorToLonLat(px, py);
      return { lon: px, lat: py };
    }

    if (native && isWebMercatorCrs(terrain.crs, native)) {
      const px = native.xCenter + (wx / worldW) * native.xSpan;
      const py = native.yCenter - (wz / worldH) * native.ySpan;
      return fromProj(px, py, native, true);
    }
    if (native && isGeographicCrs(terrain.crs)) {
      const lon = native.xCenter + (wx / worldW) * native.xSpan;
      const lat = native.yCenter - (wz / worldH) * native.ySpan;
      return { lon: lon, lat: lat };
    }
    if (wgs && !isGeographicCrs(terrain && terrain.crs)) {
      const sw = lonLatToWebMercator(wgs.west, wgs.south);
      const ne = lonLatToWebMercator(wgs.east, wgs.north);
      const xSpan = Math.max(Math.abs(ne.x - sw.x), 1e-9);
      const ySpan = Math.max(Math.abs(ne.y - sw.y), 1e-9);
      const xCenter = (sw.x + ne.x) / 2;
      const yCenter = (sw.y + ne.y) / 2;
      const px = xCenter + (wx / worldW) * xSpan;
      const py = yCenter - (wz / worldH) * ySpan;
      return webMercatorToLonLat(px, py);
    }
    if (!wgs) return { lon: 0, lat: 0 };
    return {
      lon: wgs.lonCenter + (wx / worldW) * wgs.lonSpan,
      lat: wgs.latCenter - (wz / worldH) * wgs.latSpan
    };
  }

  function sampleElevation(lon, lat, terrain) {
    if (!terrain) return NaN;
    const width = terrain.width;
    const height = terrain.height;
    const elev = decodeElevations(terrain.elevations_b64, width, height);
    const mask = decodeMask(terrain.mask_b64, width, height);
    const native = parseNativeBounds(terrain.bounds);
    const wgs = parseDemBounds(terrain.bounds_wgs84);
    let col;
    let row;
    if (native && isWebMercatorCrs(terrain.crs, native)) {
      const m = lonLatToWebMercator(lon, lat);
      col = ((m.x - native.left) / native.xSpan) * (width - 1);
      row = ((native.top - m.y) / native.ySpan) * (height - 1);
    } else if (wgs) {
      col = ((lon - wgs.west) / wgs.lonSpan) * (width - 1);
      row = ((wgs.north - lat) / wgs.latSpan) * (height - 1);
    } else {
      return Number(terrain.zmin) || 0;
    }
    col = Math.max(0, Math.min(width - 1, col));
    row = Math.max(0, Math.min(height - 1, row));
    const c0 = Math.floor(col);
    const r0 = Math.floor(row);
    const c1 = Math.min(width - 1, c0 + 1);
    const r1 = Math.min(height - 1, r0 + 1);
    const idx = function (r, c) { return r * width + c; };
    if (!mask[idx(r0, c0)]) return Number(terrain.zmin) || 0;
    const z00 = elev[idx(r0, c0)];
    const z10 = elev[idx(r0, c1)];
    const z01 = elev[idx(r1, c0)];
    const z11 = elev[idx(r1, c1)];
    const u = col - c0;
    const v = row - r0;
    return z00 * (1 - u) * (1 - v) + z10 * u * (1 - v) + z01 * (1 - u) * v + z11 * u * v;
  }

  function elevToWorldY(elev, terrain) {
    if (!terrain) return 0;
    const sizes = demWorldSize(terrain);
    const zmin = Number(terrain.zmin);
    const scale = elevationScaleForTerrain(terrain, sizes.worldW);
    return (Number(elev) - zmin) * scale;
  }

  function pickTerrain(clientX, clientY) {
    return pickGround(clientX, clientY);
  }

  function pickGround(clientX, clientY) {
    if (!renderer || !camera) return null;
    const rect = renderer.domElement.getBoundingClientRect();
    const mouse = new THREE.Vector2(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1
    );
    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(mouse, camera);
    const targets = [];
    if (currentMesh) targets.push(currentMesh);
    if (osmMesh) targets.push(osmMesh);
    const hits = targets.length ? raycaster.intersectObjects(targets, true) : [];
    let p = hits.length ? hits[0].point.clone() : null;
    if (!p) {
      const plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
      p = new THREE.Vector3();
      if (!raycaster.ray.intersectPlane(plane, p)) return null;
    }
    const ll = lastTerrain ? worldToLonLat(p.x, p.z, lastTerrain) : { lon: 0, lat: 0 };
    return {
      lon: ll.lon,
      lat: ll.lat,
      x: p.x,
      y: p.y,
      z: p.z
    };
  }

  function regionScreenPos(pt) {
    if (!camera || !renderer) return { x: 0, y: 0 };
    const v = new THREE.Vector3(pt.x, pt.y, pt.z).project(camera);
    const rect = renderer.domElement.getBoundingClientRect();
    return {
      x: (v.x * 0.5 + 0.5) * rect.width,
      y: (-v.y * 0.5 + 0.5) * rect.height
    };
  }

  function rebuildRegionDrawMesh() {
    if (!scene) return;
    if (regionDraw.group) {
      scene.remove(regionDraw.group);
      disposeObject3D(regionDraw.group);
      regionDraw.group = null;
    }
    const pts = regionDraw.points;
    if (!pts.length) {
      requestRender();
      return;
    }
    const group = new THREE.Group();
    group.name = 'regionDraw';
    const lift = 0.35;
    pts.forEach(function (pt, i) {
      const ball = new THREE.Mesh(
        new THREE.SphereGeometry(i === 0 ? 1.15 : 0.85, 12, 10),
        new THREE.MeshBasicMaterial({
          color: i === 0 ? 0xfbbf24 : 0x38bdf8,
          depthTest: false
        })
      );
      ball.position.set(pt.x, pt.y + lift, pt.z);
      ball.renderOrder = 20;
      group.add(ball);
    });
    if (pts.length >= 2) {
      const verts = pts.map(function (pt) {
        return new THREE.Vector3(pt.x, pt.y + lift, pt.z);
      });
      if (regionDraw.closed) verts.push(verts[0].clone());
      const line = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(verts),
        new THREE.LineBasicMaterial({ color: 0x38bdf8, depthTest: false })
      );
      line.renderOrder = 19;
      group.add(line);
    }
    if (regionDraw.closed && pts.length >= 3) {
      const shape = new THREE.Shape();
      shape.moveTo(pts[0].x, pts[0].z);
      for (let i = 1; i < pts.length; i++) shape.lineTo(pts[i].x, pts[i].z);
      shape.closePath();
      const fill = new THREE.Mesh(
        new THREE.ShapeGeometry(shape),
        new THREE.MeshBasicMaterial({
          color: 0x38bdf8,
          transparent: true,
          opacity: 0.22,
          side: THREE.DoubleSide,
          depthTest: false
        })
      );
      fill.rotation.x = -Math.PI / 2;
      fill.position.y = pts.reduce(function (s, p) { return s + p.y; }, 0) / pts.length + lift;
      fill.renderOrder = 18;
      group.add(fill);
    }
    regionDraw.group = group;
    scene.add(group);
    requestRender();
  }

  function unbindRegionDrawEvents() {
    if (renderer && renderer.domElement) {
      if (regionDrawOnDown) renderer.domElement.removeEventListener('pointerdown', regionDrawOnDown);
      if (regionDrawOnUp) renderer.domElement.removeEventListener('pointerup', regionDrawOnUp);
      if (regionDrawOnDbl) renderer.domElement.removeEventListener('dblclick', regionDrawOnDbl);
      renderer.domElement.style.cursor = '';
    }
    if (regionDrawOnKey) window.removeEventListener('keydown', regionDrawOnKey);
    regionDrawOnDown = regionDrawOnUp = regionDrawOnDbl = regionDrawOnKey = null;
  }

  function clearRegionDraw(silent) {
    unbindRegionDrawEvents();
    if (regionDraw.group && scene) {
      scene.remove(regionDraw.group);
      disposeObject3D(regionDraw.group);
    }
    regionDraw.group = null;
    regionDraw.active = false;
    regionDraw.closed = false;
    regionDraw.points = [];
    if (!silent) requestRender();
  }

  function closeRegionDraw() {
    if (regionDraw.points.length < 3) return false;
    regionDraw.closed = true;
    regionDraw.active = false;
    unbindRegionDrawEvents();
    rebuildRegionDrawMesh();
    return true;
  }

  function startRegionDraw() {
    if (!renderer || !scene) {
      throw new Error('Chua mo Mesh OBJ');
    }
    clearRegionDraw(true);
    regionDraw.active = true;
    regionDraw.closed = false;
    renderer.domElement.style.cursor = 'crosshair';
    regionDrawOnDown = function (ev) {
      regionDraw.downX = ev.clientX;
      regionDraw.downY = ev.clientY;
    };
    regionDrawOnUp = function (ev) {
      if (!regionDraw.active || regionDraw.closed) return;
      if (Math.hypot(ev.clientX - regionDraw.downX, ev.clientY - regionDraw.downY) > 7) return;
      const hit = pickGround(ev.clientX, ev.clientY);
      if (!hit || !Number.isFinite(hit.lon) || !Number.isFinite(hit.lat)) return;
      if (regionDraw.points.length >= 3) {
        const first = regionScreenPos(regionDraw.points[0]);
        const rect = renderer.domElement.getBoundingClientRect();
        if (Math.hypot(ev.clientX - rect.left - first.x, ev.clientY - rect.top - first.y) < 14) {
          closeRegionDraw();
          return;
        }
      }
      regionDraw.points.push(hit);
      rebuildRegionDrawMesh();
    };
    regionDrawOnDbl = function (ev) {
      ev.preventDefault();
      closeRegionDraw();
    };
    regionDrawOnKey = function (ev) {
      if (ev.key === 'Escape') {
        if (regionDraw.points.length) {
          regionDraw.points.pop();
          rebuildRegionDrawMesh();
        } else {
          clearRegionDraw();
        }
      } else if (ev.key === 'Enter') {
        closeRegionDraw();
      }
    };
    renderer.domElement.addEventListener('pointerdown', regionDrawOnDown);
    renderer.domElement.addEventListener('pointerup', regionDrawOnUp);
    renderer.domElement.addEventListener('dblclick', regionDrawOnDbl);
    window.addEventListener('keydown', regionDrawOnKey);
    requestRender();
  }

  function getRegionGeoJSON(opts) {
    opts = opts || {};
    if (opts.closeIfNeeded && !regionDraw.closed && regionDraw.points.length >= 3) {
      closeRegionDraw();
    }
    if (regionDraw.points.length < 3) {
      return { type: 'FeatureCollection', features: [] };
    }
    const ring = regionDraw.points.map(function (pt) { return [pt.lon, pt.lat]; });
    ring.push(ring[0]);
    return {
      type: 'FeatureCollection',
      features: [{
        type: 'Feature',
        properties: {},
        geometry: { type: 'Polygon', coordinates: [ring] }
      }]
    };
  }

  global.Dem3D = {
    loadDem3D: loadDem3D,
    loadPointsWgs84: loadPointsWgs84,
    parseLasBinary: parseLasBinary,
    applyTextureUrl: applyTextureUrl,
    show3DMode: show3DMode,
    destroyViewer: destroyViewer,
    setExaggeration: setExaggeration,
    setPointAppearance: setPointAppearance,
    setContoursVisible: setContoursVisible,
    pickFileIdFor3D: pickFileIdFor3D,
    getContext: function () {
      return {
        scene: scene,
        camera: camera,
        renderer: renderer,
        controls: controls,
        mesh: currentMesh,
        terrain: lastTerrain,
        exaggeration: exaggeration
      };
    },
    getViewWgs84Bounds: function () {
      if (!lastTerrain) return null;
      const win = osmViewWindow(lastTerrain);
      if (win && win.bounds) return win.bounds;
      return parseDemBounds(lastTerrain.bounds_wgs84);
    },
    lonLatToWorld: function (lon, lat) {
      if (!lastTerrain) return { x: 0, z: 0 };
      const sizes = demWorldSize(lastTerrain);
      return lonLatToWorld(lon, lat, lastTerrain, sizes.worldW, sizes.worldH);
    },
    worldToLonLat: function (wx, wz) {
      return lastTerrain ? worldToLonLat(wx, wz, lastTerrain) : { lon: 0, lat: 0 };
    },
    sampleElevation: function (lon, lat) {
      return sampleElevation(lon, lat, lastTerrain);
    },
    elevToWorldY: function (elev) {
      return elevToWorldY(elev, lastTerrain);
    },
    rasterUVToWorld: function (u, v) {
      if (!lastTerrain) return { x: 0, z: 0 };
      const sizes = demWorldSize(lastTerrain);
      return {
        x: -sizes.worldW / 2 + u * sizes.worldW,
        z: -sizes.worldH / 2 + v * sizes.worldH
      };
    },
    pickTerrain: pickTerrain,
    pickGround: pickGround,
    startRegionDraw: startRegionDraw,
    closeRegionDraw: closeRegionDraw,
    clearRegionDraw: clearRegionDraw,
    getRegionGeoJSON: getRegionGeoJSON,
    parseObjMeshBinary: parseObjMeshBinary,
    loadObjMesh: loadObjMesh
  };
})(window);
