'use strict';

const TREE_TOP = 64;
const ROOT_WIDTH = 98;
const ROOT_HEIGHT = 84;
const DRAG_DISTANCE = 4;
const ARROWS = new Map([
  ['ArrowLeft', [-1, 0]], ['ArrowRight', [1, 0]], ['ArrowUp', [0, -1]], ['ArrowDown', [0, 1]],
]);

function treeSvg(tag, attributes = {}) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
  return element;
}

function curve(source, child, gap, offset = 0, extra = false) {
  const y = source.y + offset;
  const targetY = child.y + offset;
  if (child.x <= source.right) {
    const gutter = Math.min(source.left, child.x) - 34 - Math.abs(offset);
    return `M${source.left} ${y} C${gutter} ${y} ${gutter} ${targetY} ${child.x} ${targetY}`;
  }
  // Primary curves reach the child's band within the first gutter. The rest
  // of a long edge stays in that band, clear of the sibling subtrees.
  const span = extra ? child.x - source.right : Math.min(child.x - source.right, gap * 0.8);
  const bend = source.right + span * 0.5;
  const end = source.right + span;
  return `M${source.right} ${y} C${bend} ${y} ${bend} ${targetY} ${end} ${targetY}` +
    (end < child.x ? ` H${child.x}` : '');
}

function bus(source, children, gap) {
  const forward = [];
  const paths = [];
  for (const child of children) {
    if (child.x - source.right >= 20) forward.push(child);
    else paths.push(curve(source, child, gap));
  }
  if (!forward.length) return paths.join(' ');

  forward.sort((left, right) => left.y - right.y);
  const spineX = source.right + forward.reduce((width, child) => Math.min(width, child.x - source.right), gap) / 2;
  const first = forward[0];
  const last = forward[forward.length - 1];
  const nextY = forward[1]?.y ?? source.y;
  const previousY = forward[forward.length - 2]?.y ?? source.y;
  const topRadius = Math.max(0, Math.min(10, (nextY - first.y) / 2, source.y - first.y));
  const bottomRadius = Math.max(0, Math.min(10, (last.y - previousY) / 2, last.y - source.y));
  paths.push(`M${source.right} ${source.y} H${spineX}`);
  const top = Math.min(source.y, first.y + topRadius);
  const bottom = Math.max(source.y, last.y - bottomRadius);
  if (top !== bottom) paths.push(`M${spineX} ${top} V${bottom}`);

  for (const child of forward) {
    const radius = child === first && topRadius ? topRadius : child === last ? -bottomRadius : 0;
    if (radius) {
      paths.push(`M${spineX} ${child.y + radius} Q${spineX} ${child.y} ${spineX + Math.abs(radius)} ${child.y} H${child.x}`);
    } else {
      paths.push(`M${spineX} ${child.y} H${child.x}`);
    }
  }
  return paths.join(' ');
}

class TreeCanvas {
  constructor(viewport, layout, files, storageKey, savedView) {
    this.viewport = viewport;
    this.world = viewport.querySelector('.tree-world');
    this.controls = viewport.querySelector('.tree-controls');
    this.footer = viewport.querySelector('.tree-chrome');
    this.level = this.controls.querySelector('output');
    this.key = storageKey;
    this.layout = layout;
    this.defaults = new Map(Object.entries(layout.positions));
    this.positions = new Map(this.defaults);
    this.moved = new Map();
    this.cards = new Map([...this.world.querySelectorAll('.tree-card')].map((card) => [card.dataset.path, card]));
    this.ornaments = new Map();
    this.children = new Map();
    this.routesByNode = new Map();
    this.changedNodes = new Set();
    this.changedRoutes = new Set();
    this.pointers = new Map();
    this.frame = 0;
    this.camera = {x: 0, y: 0, scale: 1};
    this.followFit = true;
    this.cameraChanged = true;
    this.suppressPointerClick = false;

    this.loadPositions();
    this.mountRoutes(files);
    this.fit();
    if (savedView?.key === this.key && !savedView.followFit) {
      this.moveCamera(savedView.camera);
    }
    this.bindEvents();
    this.resizeObserver = new ResizeObserver(() => {
      if (this.followFit) this.fit();
    });
    this.resizeObserver.observe(viewport);
    this.paint();
  }

  saveView() {
    return {key: this.key, followFit: this.followFit, camera: this.camera};
  }

  destroy() {
    this.resizeObserver.disconnect();
    cancelAnimationFrame(this.frame);
    if (this.gesture?.type === 'card' && this.gesture.moved) this.savePositions();
  }

  loadPositions() {
    try {
      const saved = JSON.parse(localStorage.getItem(this.key) || '[]');
      if (!Array.isArray(saved)) return;
      for (const entry of saved) {
        if (!Array.isArray(entry)) continue;
        const [path, point] = entry;
        if (this.defaults.has(path) && Number.isFinite(point?.x) && Number.isFinite(point?.y)) {
          this.place(path, point.x, point.y);
        }
      }
    } catch {
      // The canvas remains usable when storage is unavailable.
    }
  }

  savePositions() {
    try {
      if (this.moved.size) localStorage.setItem(this.key, JSON.stringify([...this.moved]));
      else localStorage.removeItem(this.key);
    } catch {
      // Position changes are still available in this canvas.
    }
  }

  place(path, x, y) {
    const origin = this.defaults.get(path);
    const existing = this.positions.get(path);
    if (x === existing.x && y === existing.y) return;
    if (x === origin.x && y === origin.y) {
      this.moved.delete(path);
      this.positions.set(path, origin);
    } else {
      const point = {...origin, x, y};
      this.positions.set(path, point);
      this.moved.set(path, {x, y});
    }
    this.changedNodes.add(path);
    for (const route of this.routesByNode.get(path) || []) this.changedRoutes.add(route);
  }

  mountRoutes(files) {
    const primary = this.world.querySelector('.main-edges');
    const extras = this.world.querySelector('.extra-edges');
    const details = this.world.querySelector('.tree-ports');
    const attach = (group, paths, route) => {
      route.element = treeSvg('path');
      group.append(route.element);
      this.changedRoutes.add(route);
      for (const path of paths) {
        if (path === null) continue;
        if (!this.routesByNode.has(path)) this.routesByNode.set(path, new Set());
        this.routesByNode.get(path).add(route);
      }
    };

    for (const file of files) {
      if (file.standalone) continue;
      if (!this.children.has(file.parent)) this.children.set(file.parent, []);
      this.children.get(file.parent).push(file.path);
      for (const [index, parent] of file.extraDependencies.entries()) {
        attach(extras, [parent, file.path], {parent, children: [file.path], extra: true, offset: (index % 3 - 1) * 7});
      }

      const ornament = treeSvg('g');
      ornament.append(treeSvg('circle', {
        cx: 0, cy: this.layout.cardHeight / 2, r: 3.5,
        fill: '#0f0f0f', stroke: '#7a7a7a', 'stroke-width': 2,
      }));
      const position = this.defaults.get(file.path);
      if (position.column > 0 && position.column === this.layout.columns - 1) {
        const x = this.layout.cardWidth - 7;
        const y = this.layout.cardHeight + 13;
        ornament.append(
          treeSvg('path', {d: `M${x - 25} ${y - 13} C${x - 22} ${y - 5} ${x - 12} ${y - 3} ${x - 4} ${y}`,
            fill: 'none', stroke: '#6e6e6e', 'stroke-width': 2, 'stroke-linecap': 'round'}),
          treeSvg('ellipse', {cx: x, cy: y, rx: 5, ry: 2.6, fill: '#6e6e6e', transform: `rotate(10 ${x} ${y})`}),
        );
      }
      details.append(ornament);
      this.ornaments.set(file.path, ornament);
      this.changedNodes.add(file.path);
    }

    for (const [parent, children] of this.children) {
      attach(primary, [parent, ...children], {parent, children, bus: parent === null || children.length > 4});
    }
  }

  routePath(route) {
    const root = route.parent === null;
    const parent = root ? this.layout.root : this.positions.get(route.parent);
    const source = {
      left: parent.x,
      right: parent.x + (root ? ROOT_WIDTH : this.layout.cardWidth),
      y: parent.y + (root ? ROOT_HEIGHT : this.layout.cardHeight) / 2,
    };
    const targets = route.children.map((path) => {
      const position = this.positions.get(path);
      return {x: position.x, y: position.y + this.layout.cardHeight / 2};
    });
    const gap = root ? this.layout.trunkX - source.right : this.layout.columnStep - this.layout.cardWidth;
    if (route.bus) return bus(source, targets, gap);
    return targets.map((target) => curve(source, target, gap, route.offset, route.extra)).join(' ');
  }

  schedule() {
    if (!this.frame) {
      this.frame = requestAnimationFrame(() => {
        this.frame = 0;
        this.paint();
      });
    }
  }

  paint() {
    if (this.cameraChanged) {
      const {x, y, scale} = this.camera;
      this.world.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
      this.viewport.style.backgroundPosition = `${x}px ${y}px`;
      this.viewport.style.setProperty('--grid-size', `${24 * scale}px`);
      this.viewport.style.setProperty('--grid-dot', `${scale}px`);
      this.level.textContent = Math.round(scale * 100) + '%';
      this.controls.querySelector('[data-canvas="out"]').disabled = scale <= this.minimumZoom();
      this.controls.querySelector('[data-canvas="in"]').disabled = scale >= 2;
      this.cameraChanged = false;
    }
    for (const path of this.changedNodes) {
      const {x, y} = this.positions.get(path);
      this.cards.get(path).style.transform = `translate(${x}px, ${y - TREE_TOP}px)`;
      this.ornaments.get(path)?.setAttribute('transform', `translate(${x} ${y})`);
    }
    for (const route of this.changedRoutes) route.element.setAttribute('d', this.routePath(route));
    this.changedNodes.clear();
    this.changedRoutes.clear();
  }

  bounds() {
    const estate = {left: 0, top: 64, right: this.layout.width, bottom: this.layout.height - TREE_TOP};
    for (const {x, y} of this.moved.values()) {
      estate.left = Math.min(estate.left, x - 30);
      estate.top = Math.min(estate.top, y - TREE_TOP - 8);
      estate.right = Math.max(estate.right, x + this.layout.cardWidth + 16);
      estate.bottom = Math.max(estate.bottom, y - TREE_TOP + this.layout.cardHeight + 20);
    }
    return estate;
  }

  fit() {
    const {left, top, right, bottom} = this.bounds();
    const width = this.viewport.clientWidth;
    const height = this.viewport.clientHeight;
    const contentWidth = right - left;
    const contentHeight = bottom - top;
    const header = 60;
    const footer = this.footer.offsetHeight + 28;
    const availableHeight = Math.max(1, height - header - footer);
    const scale = Math.max(0.05, Math.min(1, (width - 28) / contentWidth, availableHeight / contentHeight));
    this.fitScale = scale;
    this.followFit = true;
    this.camera = scale === 1 ?
      {x: -left, y: Math.max(0, header - top), scale} :
      {x: (width - contentWidth * scale) / 2 - left * scale,
        y: header + Math.max(0, (availableHeight - contentHeight * scale) / 2) - top * scale, scale};
    this.cameraChanged = true;
    this.schedule();
  }

  minimumZoom() {
    return Math.min(0.2, this.fitScale);
  }

  moveCamera(camera) {
    this.followFit = false;
    this.camera = camera;
    this.cameraChanged = true;
    this.schedule();
  }

  center() {
    return {x: this.viewport.clientWidth / 2, y: this.viewport.clientHeight / 2};
  }

  zoom(scale, anchor, current = this.camera, destination = anchor) {
    const next = Math.max(this.minimumZoom(), Math.min(2, scale));
    const ratio = next / current.scale;
    this.moveCamera({x: destination.x - (anchor.x - current.x) * ratio,
      y: destination.y - (anchor.y - current.y) * ratio, scale: next});
  }

  pan(x, y, camera = this.camera) {
    this.moveCamera({x: camera.x + x, y: camera.y + y, scale: camera.scale});
  }

  reset() {
    for (const path of this.moved.keys()) {
      const {x, y} = this.defaults.get(path);
      this.place(path, x, y);
    }
    this.savePositions();
    this.fit();
  }

  bindEvents() {
    const handlers = {
      pointerdown: this.pointerDown,
      pointermove: this.pointerMove,
      pointerup: this.pointerEnd,
      pointercancel: this.pointerEnd,
      lostpointercapture: this.pointerEnd,
      keydown: this.keyDown,
      focusin: this.revealFocus,
    };
    for (const [name, handler] of Object.entries(handlers)) {
      this.viewport.addEventListener(name, handler.bind(this));
    }
    this.viewport.addEventListener('wheel', this.wheel.bind(this), {passive: false});
    this.viewport.addEventListener('click', (event) => {
      if (this.suppressPointerClick && event.detail > 0 && event.target.closest('.tree-card')) {
        event.preventDefault();
        event.stopPropagation();
      }
    }, true);
    this.controls.addEventListener('click', (event) => {
      const action = event.target.closest('button')?.dataset.canvas;
      if (action === 'fit') this.fit();
      if (action === 'reset') this.reset();
      if (action === 'in') this.zoom(this.camera.scale * 1.25, this.center());
      if (action === 'out') this.zoom(this.camera.scale / 1.25, this.center());
    });
  }

  point(event) {
    const {left, top} = this.viewport.getBoundingClientRect();
    return {x: event.clientX - left, y: event.clientY - top};
  }

  pinchPoints() {
    const [a, b] = this.pointers.values();
    return {center: {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2}, distance: Math.hypot(b.x - a.x, b.y - a.y)};
  }

  subtree(path) {
    const list = [path];
    for (let i = 0; i < list.length; i++) list.push(...(this.children.get(list[i]) || []));
    return list;
  }

  pointerDown(event) {
    if (event.button !== 0 || this.pointers.size >= 2 || event.target.closest('.tree-controls')) return;
    const card = event.target.closest('.tree-card');
    const point = this.point(event);
    if (!this.pointers.size) this.suppressPointerClick = false;
    this.pointers.set(event.pointerId, point);
    (card || this.viewport).setPointerCapture(event.pointerId);

    if (this.pointers.size === 2) {
      this.endDrag();
      this.suppressPointerClick = true;
      this.gesture = {type: 'pinch', start: this.pinchPoints(), camera: this.camera};
    } else if (card) {
      const path = card.dataset.path;
      const paths = event.shiftKey ? this.subtree(path) : [path];
      const starts = new Map(paths.map((item) => [item, this.positions.get(item)]));
      this.gesture = {type: 'card', point, camera: this.camera, card, starts, moved: false};
    } else {
      this.viewport.focus({preventScroll: true});
      this.gesture = {type: 'pan', point, camera: this.camera, moved: false};
    }
  }

  pointerMove(event) {
    const gesture = this.gesture;
    if (!gesture || !this.pointers.has(event.pointerId)) return;
    const point = this.point(event);
    this.pointers.set(event.pointerId, point);
    if (gesture.type === 'pinch') {
      const current = this.pinchPoints();
      const ratio = current.distance / Math.max(1, gesture.start.distance);
      this.zoom(gesture.camera.scale * ratio, gesture.start.center, gesture.camera, current.center);
      return;
    }

    const dx = point.x - gesture.point.x;
    const dy = point.y - gesture.point.y;
    if (!gesture.moved && Math.hypot(dx, dy) < DRAG_DISTANCE) return;
    if (!gesture.moved) {
      gesture.moved = true;
      this.suppressPointerClick = true;
      this.viewport.classList.add('dragging');
      gesture.card?.classList.add('moving');
    }
    if (gesture.type === 'pan') {
      this.pan(dx, dy, gesture.camera);
    } else {
      for (const [path, start] of gesture.starts) {
        this.place(path, start.x + dx / gesture.camera.scale, start.y + dy / gesture.camera.scale);
      }
      this.schedule();
    }
  }

  endDrag() {
    if (this.gesture?.type === 'card' && this.gesture.moved) this.savePositions();
    this.gesture?.card?.classList.remove('moving');
    this.viewport.classList.remove('dragging');
  }

  pointerEnd(event) {
    if (!this.pointers.has(event.pointerId)) return;
    if (event.type === 'pointerup') this.pointerMove(event);
    this.pointers.delete(event.pointerId);
    this.endDrag();
    this.gesture = this.pointers.size ?
      {type: 'pan', point: this.pointers.values().next().value, camera: this.camera, moved: true} : null;
  }

  wheel(event) {
    event.preventDefault();
    if (this.pointers.size) return;
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? this.viewport.clientHeight : 1;
    if (event.ctrlKey || event.metaKey) {
      this.zoom(this.camera.scale * Math.exp(-event.deltaY * unit * 0.0025), this.point(event));
    } else {
      this.pan(-event.deltaX * unit, -event.deltaY * unit);
    }
  }

  keyDown(event) {
    const direction = ARROWS.get(event.key);
    const card = event.target.closest('.tree-card');
    if (event.altKey && card && direction) {
      event.preventDefault();
      const path = card.dataset.path;
      const {x, y} = this.positions.get(path);
      this.place(path, x + direction[0] * 16, y + direction[1] * 16);
      this.savePositions();
      this.schedule();
    } else if (event.target === this.viewport && !event.altKey && !event.ctrlKey && !event.metaKey) {
      if (direction) this.pan(-direction[0] * 64, -direction[1] * 64);
      else if (event.key === '+' || event.key === '=') this.zoom(this.camera.scale * 1.25, this.center());
      else if (event.key === '-' || event.key === '−') this.zoom(this.camera.scale / 1.25, this.center());
      else if (event.key === '0') this.fit();
      else return;
      event.preventDefault();
    }
  }

  revealFocus(event) {
    const card = event.target.closest('.tree-card');
    if (!card || this.pointers.size || !card.matches(':focus-visible')) return;
    const {left, right, top, bottom} = card.getBoundingClientRect();
    const viewport = this.viewport.getBoundingClientRect();
    const dx = Math.max(0, viewport.left + 12 - left) + Math.min(0, viewport.right - 12 - right);
    const dy = Math.max(0, viewport.top + 54 - top) + Math.min(0, viewport.bottom - 62 - bottom);
    if (dx || dy) this.pan(dx, dy);
  }
}
