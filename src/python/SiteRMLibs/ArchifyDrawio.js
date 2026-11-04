/*
 * draw.io export for Archify architecture diagrams.
 *
 * Adds "draw.io" entries to the diagram's Export menu. The file is built from
 * the diagram as it is drawn on the page, not from the IR: boxes, groups,
 * every link with its real route, labels, rings and status dots, the legend
 * and the cards, all in the colours the page computes for the chosen theme.
 * So it follows whatever the host page has added or restyled, and it needs no
 * knowledge of where the diagram came from.
 *
 * Plain script, no dependencies; safe in a sandboxed iframe (no storage, no
 * same-origin access). Download uses an <a download> click, which the sandbox
 * allows with allow-downloads.
 */
(function () {
  "use strict";
  if (window.ArchifyDrawio) return;

  var CARD_W = 300;
  var CARD_ITEMS = 14;
  var GAP = 40;

  function esc(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/\n/g, "&#10;");
  }

  function html(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // ---- colours ------------------------------------------------------------

  function parse(value) {
    // color-mix() and friends compute to color(srgb r g b / a), channels 0..1
    var c =
      /color\(srgb\s+([\d.e-]+)\s+([\d.e-]+)\s+([\d.e-]+)(?:\s*\/\s*([\d.]+%?))?\s*\)/.exec(
        value || "",
      );
    if (c) {
      var ca =
        c[4] === undefined
          ? 1
          : c[4].slice(-1) === "%"
            ? parseFloat(c[4]) / 100
            : parseFloat(c[4]);
      return { r: +c[1] * 255, g: +c[2] * 255, b: +c[3] * 255, a: ca };
    }
    var m =
      /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)/.exec(
        value || "",
      );
    if (!m) return null;
    var a =
      m[4] === undefined
        ? 1
        : m[4].slice(-1) === "%"
          ? parseFloat(m[4]) / 100
          : parseFloat(m[4]);
    return { r: +m[1], g: +m[2], b: +m[3], a: a };
  }

  function hex(c) {
    function two(v) {
      return ("0" + Math.round(v).toString(16)).slice(-2);
    }
    return "#" + two(c.r) + two(c.g) + two(c.b);
  }

  // A computed colour as opaque hex, composited over a backdrop when it is
  // translucent. null when there is nothing to paint.
  function solid(value, backdrop) {
    var c = parse(value);
    if (!c || c.a === 0) return null;
    if (c.a >= 1 || !backdrop) return hex(c);
    var b = parse(backdrop) || { r: 255, g: 255, b: 255 };
    return hex({
      r: c.r * c.a + b.r * (1 - c.a),
      g: c.g * c.a + b.g * (1 - c.a),
      b: c.b * c.a + b.b * (1 - c.a),
    });
  }

  function alpha(value) {
    var c = parse(value);
    return c ? c.a : 1;
  }

  // ---- geometry -----------------------------------------------------------

  function matrixTo(svg, el) {
    var root = svg.getScreenCTM();
    var own = el.getScreenCTM();
    if (!root || !own) return null;
    return root.inverse().multiply(own);
  }

  function apply(m, x, y) {
    return m
      ? { x: m.a * x + m.c * y + m.e, y: m.b * x + m.d * y + m.f }
      : { x: x, y: y };
  }

  function boxOf(svg, el) {
    var b = el.getBBox();
    var m = matrixTo(svg, el);
    var p = apply(m, b.x, b.y);
    var q = apply(m, b.x + b.width, b.y + b.height);
    return {
      x: Math.min(p.x, q.x),
      y: Math.min(p.y, q.y),
      w: Math.abs(q.x - p.x),
      h: Math.abs(q.y - p.y),
    };
  }

  // The corners of an SVG path, in SVG user units. Curves contribute their
  // control point (rounded corners of orthogonal routes) and end point.
  function pathPoints(svg, el) {
    var d = el.getAttribute("d") || "";
    var tokens = d.match(/[a-zA-Z]|-?\d*\.?\d+(?:e[-+]?\d+)?/g) || [];
    var pts = [];
    var cmd = "M",
      i = 0,
      x = 0,
      y = 0,
      sx = 0,
      sy = 0;
    function num() {
      return parseFloat(tokens[i++]);
    }
    while (i < tokens.length) {
      if (/[a-zA-Z]/.test(tokens[i])) cmd = tokens[i++];
      var rel = cmd === cmd.toLowerCase();
      var C = cmd.toUpperCase();
      var ox = rel ? x : 0,
        oy = rel ? y : 0;
      if (C === "Z") {
        pts.push({ x: sx, y: sy });
        x = sx;
        y = sy;
        continue;
      }
      if (C === "M" || C === "L" || C === "T") {
        x = ox + num();
        y = oy + num();
        if (C === "M") {
          sx = x;
          sy = y;
          if (cmd === "M") cmd = "L";
          else cmd = "l";
        }
        pts.push({ x: x, y: y });
      } else if (C === "H") {
        x = ox + num();
        pts.push({ x: x, y: y });
      } else if (C === "V") {
        y = oy + num();
        pts.push({ x: x, y: y });
      } else if (C === "Q") {
        var qx = ox + num(),
          qy = oy + num();
        x = ox + num();
        y = oy + num();
        pts.push({ x: qx, y: qy }, { x: x, y: y });
      } else if (C === "C" || C === "S") {
        if (C === "C") {
          num();
          num();
        }
        num();
        num();
        x = ox + num();
        y = oy + num();
        pts.push({ x: x, y: y });
      } else if (C === "A") {
        num();
        num();
        num();
        num();
        num();
        x = ox + num();
        y = oy + num();
        pts.push({ x: x, y: y });
      } else {
        i++;
      }
    }
    var m = matrixTo(svg, el);
    return corners(
      pts.map(function (p) {
        return apply(m, p.x, p.y);
      }),
    );
  }

  // Drop repeated points and points on a straight run, keep the ends.
  function corners(pts) {
    var out = [];
    pts.forEach(function (p) {
      var last = out[out.length - 1];
      if (!last || Math.abs(last.x - p.x) > 0.5 || Math.abs(last.y - p.y) > 0.5)
        out.push(p);
    });
    return out.filter(function (p, k) {
      if (k === 0 || k === out.length - 1) return true;
      var a = out[k - 1],
        b = out[k + 1];
      var cross = (p.x - a.x) * (b.y - a.y) - (p.y - a.y) * (b.x - a.x);
      return Math.abs(cross) > 0.5 * Math.hypot(b.x - a.x, b.y - a.y);
    });
  }

  function inside(p, box, slack) {
    return (
      p.x >= box.x - slack &&
      p.x <= box.x + box.w + slack &&
      p.y >= box.y - slack &&
      p.y <= box.y + box.h + slack
    );
  }

  // Where along a polyline (-1..1, draw.io's label position) a point sits,
  // and its offset from that spot.
  function along(pts, p) {
    var lens = [],
      total = 0;
    for (var k = 1; k < pts.length; k++) {
      var l = Math.hypot(pts[k].x - pts[k - 1].x, pts[k].y - pts[k - 1].y);
      lens.push(l);
      total += l;
    }
    var best = { d: Infinity, at: 0, q: pts[0] },
      walked = 0;
    for (k = 0; k < lens.length; k++) {
      var a = pts[k],
        b = pts[k + 1],
        len = lens[k] || 1;
      var t = Math.max(
        0,
        Math.min(
          1,
          ((p.x - a.x) * (b.x - a.x) + (p.y - a.y) * (b.y - a.y)) / (len * len),
        ),
      );
      var q = { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
      var dist = Math.hypot(p.x - q.x, p.y - q.y);
      if (dist < best.d) best = { d: dist, at: walked + lens[k] * t, q: q };
      walked += lens[k];
    }
    return {
      x: total ? (best.at / total) * 2 - 1 : 0,
      dx: p.x - best.q.x,
      dy: p.y - best.q.y,
    };
  }

  // ---- reading the page ---------------------------------------------------

  function style(el) {
    return getComputedStyle(el);
  }

  function textOf(el) {
    return el ? (el.textContent || "").replace(/\s+/g, " ").trim() : "";
  }

  // The page's font stack for draw.io: its own families first (draw.io passes
  // the value to CSS), generic keywords dropped, Helvetica as the fallback so
  // a missing web font never ends up as the browser's serif default.
  function fontName(el) {
    var families = style(el)
      .fontFamily.split(",")
      .map(function (f) {
        return f.replace(/["']/g, "").trim();
      })
      .filter(function (f) {
        return (
          f &&
          !/^(system-ui|-apple-system|BlinkMacSystemFont|ui-sans-serif|ui-monospace|sans-serif|serif|monospace)$/i.test(
            f,
          )
        );
      });
    if (families.indexOf("Helvetica") < 0) families.push("Helvetica");
    return families.slice(0, 4).join(",");
  }

  function dash(st, width) {
    var arr = st.strokeDasharray;
    if (!arr || arr === "none") return "";
    var parts = arr
      .split(/[\s,]+/)
      .map(parseFloat)
      .filter(function (v) {
        return v > 0;
      });
    if (!parts.length) return "";
    var w = Math.max(width, 0.5);
    return (
      "dashed=1;dashPattern=" +
      parts
        .map(function (v) {
          return +(v / w).toFixed(2);
        })
        .join(" ") +
      ";"
    );
  }

  function arrow(st, side) {
    var marker = side === "end" ? st.markerEnd : st.markerStart;
    return marker && marker !== "none"
      ? side + "Arrow=block;" + side + "Fill=1;" + side + "Size=5;"
      : side + "Arrow=none;";
  }

  function Builder(svg, theme) {
    this.svg = svg;
    this.cells = [];
    this.next = 2;
    this.ids = {};
    this.boxes = {};
    this.theme = theme;
    var container = svg.closest(".diagram-container") || document.body;
    this.paper = style(container).backgroundColor;
    if (alpha(this.paper) === 0)
      this.paper = style(document.body).backgroundColor;
    if (alpha(this.paper) === 0)
      this.paper = theme === "dark" ? "rgb(17, 18, 23)" : "rgb(255, 255, 255)";
    var sample = svg.querySelector("[data-node-id] text") || svg;
    this.font = fontName(sample);
    var primary = svg.querySelector(".t-primary");
    var muted = svg.querySelector(".t-muted");
    this.ink =
      solid(
        primary ? style(primary).fill : style(document.body).color,
        this.paper,
      ) || "#000000";
    this.muted =
      solid(
        muted ? style(muted).fill : style(document.body).color,
        this.paper,
      ) || this.ink;
    var mask = svg.querySelector("rect.c-mask");
    this.mask = mask ? style(mask).fill : this.paper;
  }

  Builder.prototype.id = function (key) {
    var id = "c" + this.next++;
    if (key) this.ids[key] = id;
    return id;
  };

  Builder.prototype.vertex = function (box, value, css, key, tooltip) {
    var id = this.id(key);
    this.cells.push(
      '<mxCell id="' +
        id +
        '" value="' +
        esc(value) +
        '" style="' +
        css +
        '" vertex="1" parent="1"' +
        (tooltip ? ' tooltip="' + esc(tooltip) + '"' : "") +
        '><mxGeometry x="' +
        r(box.x) +
        '" y="' +
        r(box.y) +
        '" width="' +
        r(box.w) +
        '" height="' +
        r(box.h) +
        '" as="geometry"/></mxCell>',
    );
    if (key) this.boxes[key] = box;
    return id;
  };

  function r(v) {
    return Math.round(v * 10) / 10;
  }

  Builder.prototype.text = function (box, value, opts) {
    opts = opts || {};
    return this.vertex(
      box,
      value,
      "text;html=1;strokeColor=none;fillColor=none;whiteSpace=wrap;overflow=visible;" +
        "align=" +
        (opts.align || "left") +
        ";verticalAlign=" +
        (opts.valign || "middle") +
        ";spacing=0;" +
        "fontFamily=" +
        this.font +
        ";fontSize=" +
        (opts.size || 11) +
        ";fontColor=" +
        (opts.color || this.ink) +
        ";" +
        (opts.bold ? "fontStyle=1;" : ""),
    );
  };

  // Group frames (sites, regions) go first so everything sits on top of them.
  Builder.prototype.frames = function () {
    var self = this;
    this.svg
      .querySelectorAll(
        'rect[data-graph-role="structural-frame"], [data-boundary-id] > rect:not(.c-mask)',
      )
      .forEach(function (rect) {
        var st = style(rect);
        var box = boxOf(self.svg, rect);
        var label =
          rect.getAttribute("data-composition-frame-label") ||
          (rect.parentNode.getAttribute &&
            rect.parentNode.getAttribute("data-boundary-label")) ||
          "";
        var id =
          rect.getAttribute("data-composition-frame-id") ||
          rect.parentNode.getAttribute("data-boundary-id");
        var tag = self.svg.querySelector(
          '[data-graph-role="structural-frame-label"][data-composition-frame-id="' +
            id +
            '"] text, [data-boundary-id="' +
            id +
            '"] text',
        );
        var width = parseFloat(st.strokeWidth) || 1;
        var rx = parseFloat(rect.getAttribute("rx")) || 0;
        self.vertex(
          box,
          label,
          "rounded=1;absoluteArcSize=1;arcSize=" +
            rx * 2 +
            ";html=1;whiteSpace=wrap;container=0;" +
            "fillColor=" +
            (solid(st.fill, self.paper) || "none") +
            ";strokeColor=" +
            (solid(st.stroke, self.paper) || "none") +
            ";" +
            "strokeWidth=" +
            width +
            ";" +
            dash(st, width) +
            "verticalAlign=top;align=left;spacingLeft=10;spacingTop=6;fontStyle=1;fontFamily=" +
            self.font +
            ";" +
            "fontSize=" +
            (tag ? parseFloat(style(tag).fontSize) || 10 : 10) +
            ";fontColor=" +
            (tag ? solid(style(tag).fill, self.paper) : self.ink) +
            ";",
        );
      });
  };

  Builder.prototype.nodes = function () {
    var self = this;
    this.svg.querySelectorAll("g[data-node-id]").forEach(function (g) {
      var rect = g.querySelector(
        ":scope > rect:not(.c-mask), :scope > path:not(.c-mask), :scope > ellipse, :scope > circle",
      );
      if (!rect) return;
      var st = style(rect);
      var box = boxOf(self.svg, rect);
      var under = style(g.querySelector(":scope > .c-mask") || rect).fill;
      var fill =
        solid(st.fill, solid(under, self.paper) ? under : self.paper) ||
        solid(under, self.paper) ||
        "none";
      var width = parseFloat(st.strokeWidth) || 1;
      var label =
        g.getAttribute("data-node-label") ||
        textOf(g.querySelector("[data-node-label]"));
      var lines = ["<b>" + html(label) + "</b>"];
      g.querySelectorAll("text[data-detail]").forEach(function (t) {
        var full =
          t.getAttribute("data-detail") === "context"
            ? g.getAttribute("data-node-sublabel") || textOf(t)
            : textOf(t);
        if (!full) return;
        lines.push(
          '<span style="font-size:' +
            (parseFloat(style(t).fontSize) || 9) +
            "px;color:" +
            solid(style(t).fill, fill) +
            '">' +
            html(full) +
            "</span>",
        );
      });
      var primary = g.querySelector("[data-node-label]");
      var shape =
        rect.tagName === "ellipse" || rect.tagName === "circle"
          ? "ellipse;"
          : "rounded=1;absoluteArcSize=1;arcSize=" +
            (parseFloat(rect.getAttribute("rx")) || 0) * 2 +
            ";";
      self.vertex(
        box,
        lines.join("<br>"),
        shape +
          "html=1;whiteSpace=wrap;fillColor=" +
          fill +
          ";strokeColor=" +
          (solid(st.stroke, self.paper) || "none") +
          ";" +
          "strokeWidth=" +
          width +
          ";" +
          dash(st, width) +
          "fontFamily=" +
          self.font +
          ";fontSize=" +
          (primary ? parseFloat(style(primary).fontSize) || 11 : 11) +
          ";" +
          "fontColor=" +
          (primary ? solid(style(primary).fill, fill) : self.ink) +
          ";spacing=4;",
        g.getAttribute("data-node-id"),
        g.getAttribute("data-node-context") &&
          g.getAttribute("data-node-context") !== "Architecture component"
          ? g.getAttribute("data-node-context")
          : "",
      );
    });
  };

  // Every drawn link: one draw.io edge per SVG path, attached to its two
  // boxes, with the route's corners as waypoints.
  Builder.prototype.edges = function () {
    var self = this;
    var labelled = {};
    var paths = this.svg.querySelectorAll("path[data-edge-from][data-edge-to]");
    paths.forEach(function (path) {
      var from = path.getAttribute("data-edge-from"),
        to = path.getAttribute("data-edge-to");
      var source = self.ids[from],
        target = self.ids[to];
      var pts = pathPoints(self.svg, path);
      if (pts.length < 2) return;
      var a = self.boxes[from],
        b = self.boxes[to];
      if (a && b && !inside(pts[0], a, 3) && inside(pts[0], b, 3)) {
        var tmp = source;
        source = target;
        target = tmp;
        var box = a;
        a = b;
        b = box;
        pts.reverse();
      }
      var st = style(path);
      var width = parseFloat(st.strokeWidth) || 1;
      var stroke = solid(st.stroke, self.paper);
      if (!stroke) return;
      var css =
        "html=1;edgeStyle=none;rounded=1;arcSize=12;" +
        arrow(st, "end") +
        arrow(st, "start") +
        "strokeColor=" +
        stroke +
        ";strokeWidth=" +
        width +
        ";" +
        dash(st, width);
      var opacity = parseFloat(st.opacity) * alpha(st.stroke);
      if (opacity < 0.99) css += "opacity=" + Math.round(opacity * 100) + ";";
      function port(p, box, side) {
        if (!box) return "";
        var fx = Math.max(0, Math.min(1, (p.x - box.x) / (box.w || 1)));
        var fy = Math.max(0, Math.min(1, (p.y - box.y) / (box.h || 1)));
        return (
          side +
          "X=" +
          r(fx * 1000) / 1000 +
          ";" +
          side +
          "Y=" +
          r(fy * 1000) / 1000 +
          ";" +
          side +
          "Perimeter=0;"
        );
      }
      css += port(pts[0], a, "exit") + port(pts[pts.length - 1], b, "entry");

      var key = path.getAttribute("data-edge-id") || from + ">" + to;
      var value = "",
        geo = "";
      var tag = labelled[key]
        ? null
        : self.svg.querySelector(
            'g[data-edge-label][data-edge-id="' + cssEscape(key) + '"]',
          );
      if (tag && textOf(tag)) {
        labelled[key] = true;
        var t = tag.querySelector("text");
        var chip = tag.querySelector("rect");
        var centre = boxOf(self.svg, chip || t);
        var spot = along(pts, {
          x: centre.x + centre.w / 2,
          y: centre.y + centre.h / 2,
        });
        var chipFill = chip ? solid(style(chip).fill, self.paper) : null;
        value = textOf(tag);
        css +=
          "fontFamily=" +
          self.font +
          ";fontSize=" +
          (parseFloat(style(t).fontSize) || 8) +
          ";fontColor=" +
          solid(style(t).fill, chipFill || self.paper) +
          ";" +
          "labelBackgroundColor=" +
          (chipFill || "none") +
          ";";
        geo = ' x="' + r(spot.x * 1000) / 1000 + '"';
        var offset =
          '<mxPoint x="' +
          r(spot.dx) +
          '" y="' +
          r(spot.dy) +
          '" as="offset"/>';
        geo = [geo, offset];
      }
      var titleEl = path.querySelector("title");
      var tooltip = titleEl
        ? textOf(titleEl)
        : path.getAttribute("data-edge-label") || "";
      var id = self.id();
      var inner =
        (geo ? geo[1] : "") +
        (pts.length > 2
          ? '<Array as="points">' +
            pts
              .slice(1, -1)
              .map(function (p) {
                return '<mxPoint x="' + r(p.x) + '" y="' + r(p.y) + '"/>';
              })
              .join("") +
            "</Array>"
          : "");
      var ends =
        (source ? ' source="' + source + '"' : "") +
        (target ? ' target="' + target + '"' : "");
      var loose =
        (source
          ? ""
          : '<mxPoint x="' +
            r(pts[0].x) +
            '" y="' +
            r(pts[0].y) +
            '" as="sourcePoint"/>') +
        (target
          ? ""
          : '<mxPoint x="' +
            r(pts[pts.length - 1].x) +
            '" y="' +
            r(pts[pts.length - 1].y) +
            '" as="targetPoint"/>');
      self.cells.push(
        '<mxCell id="' +
          id +
          '" value="' +
          esc(value) +
          '" style="' +
          css +
          '" edge="1" parent="1"' +
          ends +
          (tooltip ? ' tooltip="' + esc(tooltip) + '"' : "") +
          "><mxGeometry" +
          (geo ? geo[0] : "") +
          ' relative="1" as="geometry">' +
          loose +
          inner +
          "</mxGeometry></mxCell>",
      );
    });
  };

  function cssEscape(v) {
    return window.CSS && CSS.escape
      ? CSS.escape(v)
      : String(v).replace(/["\\]/g, "\\$&");
  }

  // Marks a host page draws on top (port-channel rings, status dots, ...):
  // any ellipse or circle outside the boxes, legend and icons.
  Builder.prototype.marks = function () {
    var self = this;
    this.svg.querySelectorAll("ellipse, circle").forEach(function (el) {
      if (
        el.closest(
          "defs, marker, pattern, [data-node-id], [data-legend], .semantic-sigil, [data-semantic-sigil]",
        )
      )
        return;
      var st = style(el);
      if (st.display === "none" || st.visibility === "hidden") return;
      var fill = solid(st.fill, self.paper),
        stroke = solid(st.stroke, self.paper);
      if (!fill && !stroke) return;
      var width = parseFloat(st.strokeWidth) || 1;
      var titleEl = el.querySelector("title");
      self.vertex(
        boxOf(self.svg, el),
        "",
        "ellipse;html=1;fillColor=" +
          (fill || "none") +
          ";strokeColor=" +
          (stroke || "none") +
          ";strokeWidth=" +
          width +
          ";",
        null,
        titleEl ? textOf(titleEl) : "",
      );
    });
  };

  Builder.prototype.legend = function () {
    var self = this;
    this.svg.querySelectorAll("[data-legend]").forEach(function (legend) {
      legend.querySelectorAll("rect, text").forEach(function (el) {
        var st = style(el);
        var box = boxOf(self.svg, el);
        if (el.tagName === "text") {
          var txt = textOf(el);
          if (txt)
            self.text(
              { x: box.x, y: box.y, w: box.w + 8, h: box.h },
              html(txt),
              {
                size: parseFloat(st.fontSize) || 10,
                color: solid(st.fill, self.paper),
                bold: parseInt(st.fontWeight, 10) >= 600,
              },
            );
          return;
        }
        var width = parseFloat(st.strokeWidth) || 1;
        self.vertex(
          box,
          "",
          "rounded=1;absoluteArcSize=1;arcSize=" +
            (parseFloat(el.getAttribute("rx")) || 0) * 2 +
            ";html=1;" +
            "fillColor=" +
            (solid(st.fill, self.paper) || "none") +
            ";strokeColor=" +
            (solid(st.stroke, self.paper) || "none") +
            ";strokeWidth=" +
            width +
            ";" +
            dash(st, width),
        );
      });
    });
  };

  // Title, subtitle and the info cards, laid out around the diagram.
  Builder.prototype.chrome = function (bounds) {
    var self = this;
    var title = textOf(document.querySelector(".header h1, header h1, h1"));
    var subtitle = textOf(
      document.querySelector(".header .subtitle, .subtitle, header p"),
    );
    if (title)
      this.text(
        { x: bounds.x, y: bounds.y - 64, w: Math.max(bounds.w, 400), h: 28 },
        html(title),
        { size: 20, bold: true },
      );
    if (subtitle)
      this.text(
        { x: bounds.x, y: bounds.y - 34, w: Math.max(bounds.w, 400), h: 18 },
        html(subtitle),
        { size: 12, color: this.muted },
      );

    var cards = document.querySelectorAll(".cards .card");
    if (!cards.length) return;
    var cols = Math.max(
      1,
      Math.floor((Math.max(bounds.w, CARD_W) + GAP) / (CARD_W + GAP)),
    );
    var heights = [];
    for (var c = 0; c < cols; c++) heights.push(bounds.y + bounds.h + GAP);
    var panel = document.querySelector(".cards .card");
    var cst = style(panel);
    var cardFill = solid(cst.backgroundColor, this.paper) || solid(this.paper);
    var cardLine = solid(cst.borderTopColor, this.paper) || this.muted;
    cards.forEach(function (card) {
      var col = heights.indexOf(Math.min.apply(null, heights));
      var x = bounds.x + col * (CARD_W + GAP);
      var y = heights[col];
      var head = textOf(card.querySelector("h3, .card-header"));
      var dot = card.querySelector(".card-dot");
      var dotColour = dot ? solid(style(dot).backgroundColor, cardFill) : null;
      var items = Array.prototype.slice.call(card.querySelectorAll("li"));
      var shown = items.slice(0, CARD_ITEMS);
      var rows = [];
      shown.forEach(function (li) {
        var swatch = li.querySelector("span");
        var sw = swatch ? style(swatch) : null;
        var text = textOf(li).replace(/^[•\u2022]\s*/, "");
        var mark = null;
        if (sw && swatch !== li && !textOf(swatch)) {
          // a line swatch is a zero-height box drawn by its top border (its
          // reported height may include the border under border-box sizing)
          var rect = swatch.getBoundingClientRect();
          var line =
            parseFloat(sw.borderTopWidth) > 0 &&
            sw.borderTopStyle !== "none" &&
            rect.width > rect.height * 3;
          mark = line
            ? {
                kind: "line",
                colour: solid(sw.borderTopColor, cardFill),
                width: parseFloat(sw.borderTopWidth),
                style: sw.borderTopStyle,
              }
            : {
                kind: "box",
                fill: solid(sw.backgroundColor, cardFill),
                stroke:
                  parseFloat(sw.borderTopWidth) > 0
                    ? solid(sw.borderTopColor, cardFill)
                    : null,
                round: parseFloat(sw.borderTopLeftRadius) > 3,
              };
        }
        rows.push({ text: text, mark: mark });
      });
      if (items.length > shown.length)
        rows.push({
          text: "\u2026 and " + (items.length - shown.length) + " more",
          mark: null,
        });
      var lineH = 13;
      var top = y + 34;
      rows.forEach(function (row) {
        var room =
          CARD_W - 22 - (row.mark ? (row.mark.kind === "line" ? 32 : 22) : 0);
        row.lines = wrapCount(row.text, room, "10px " + self.font);
        row.y = top;
        top += row.lines * lineH + 3;
      });
      var h = top - y + 8;
      self.vertex(
        { x: x, y: y, w: CARD_W, h: h },
        "",
        "rounded=1;absoluteArcSize=1;arcSize=16;html=1;fillColor=" +
          cardFill +
          ";strokeColor=" +
          cardLine +
          ";strokeWidth=1;",
      );
      if (dotColour)
        self.vertex(
          { x: x + 12, y: y + 13, w: 8, h: 8 },
          "",
          "ellipse;html=1;fillColor=" + dotColour + ";strokeColor=none;",
        );
      self.text(
        { x: x + (dotColour ? 28 : 12), y: y + 8, w: CARD_W - 40, h: 18 },
        html(head),
        { size: 12, bold: true },
      );
      rows.forEach(function (row) {
        var ry = row.y;
        var tx = x + 12;
        if (row.mark && row.mark.kind === "line") {
          var dashed =
            row.mark.style === "dashed"
              ? "dashed=1;dashPattern=3 2;"
              : row.mark.style === "dotted"
                ? "dashed=1;dashPattern=1 3;"
                : "";
          self.vertex(
            { x: tx, y: ry + lineH / 2 - 1, w: 24, h: 2 },
            "",
            "line;html=1;strokeColor=" +
              row.mark.colour +
              ";strokeWidth=" +
              Math.max(row.mark.width, 1) +
              ";" +
              dashed,
          );
          tx += 32;
        } else if (row.mark) {
          self.vertex(
            { x: tx + 2, y: ry + 1.5, w: 10, h: 10 },
            "",
            (row.mark.round ? "ellipse;" : "rounded=1;") +
              "html=1;fillColor=" +
              (row.mark.fill || "none") +
              ";strokeColor=" +
              (row.mark.stroke || "none") +
              ";",
          );
          tx += 22;
        }
        self.text(
          { x: tx, y: ry, w: x + CARD_W - 10 - tx, h: row.lines * lineH },
          html(row.text),
          { size: 10, color: self.muted, valign: "top" },
        );
      });
      heights[col] = y + h + 16;
    });
  };

  // How many lines a text wraps to in a given width (draw.io wraps on spaces
  // like the browser does, so measuring with the same font is close enough).
  var measure = null;
  function wrapCount(text, width, font) {
    measure = measure || document.createElement("canvas").getContext("2d");
    measure.font = font;
    var lines = 1,
      current = "";
    String(text)
      .split(/\s+/)
      .forEach(function (word) {
        var next = current ? current + " " + word : word;
        if (current && measure.measureText(next).width > width) {
          lines += 1 + Math.floor(measure.measureText(word).width / width);
          current = word;
        } else {
          current = next;
        }
      });
    return lines;
  }

  Builder.prototype.bounds = function () {
    var box = null;
    var self = this;
    this.svg
      .querySelectorAll(
        'g[data-node-id], [data-legend], rect[data-graph-role="structural-frame"], path[data-edge-from]',
      )
      .forEach(function (el) {
        var b = boxOf(self.svg, el);
        if (!b.w && !b.h) return;
        box = box
          ? {
              x: Math.min(box.x, b.x),
              y: Math.min(box.y, b.y),
              x2: Math.max(box.x2, b.x + b.w),
              y2: Math.max(box.y2, b.y + b.h),
            }
          : { x: b.x, y: b.y, x2: b.x + b.w, y2: b.y + b.h };
      });
    box = box || { x: 0, y: 0, x2: 800, y2: 600 };
    return { x: box.x, y: box.y, w: box.x2 - box.x, h: box.y2 - box.y };
  };

  // Build the .drawio XML for the diagram in the given theme ("light",
  // "dark", or anything else for the theme on screen).
  function build(theme) {
    var svg =
      document.querySelector(".diagram-container > svg") ||
      document.querySelector("svg[viewBox]");
    if (!svg) throw new Error("no diagram on this page");
    var root = document.documentElement;
    var was = root.getAttribute("data-theme");
    var freeze = document.createElement("style");
    freeze.textContent =
      "*, *::before, *::after { transition: none !important; animation: none !important; }";
    document.head.appendChild(freeze);
    if (theme === "light" || theme === "dark")
      root.setAttribute("data-theme", theme);
    try {
      var b = new Builder(svg, root.getAttribute("data-theme"));
      var bounds = b.bounds();
      b.frames();
      b.nodes();
      b.edges();
      b.marks();
      b.legend();
      b.chrome(bounds);
      var name =
        textOf(document.querySelector(".header h1, header h1, h1")) ||
        document.title ||
        "diagram";
      var model =
        '<mxGraphModel dx="0" dy="0" grid="0" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="0" pageScale="1" math="0" shadow="0" background="' +
        (solid(b.paper) || "#ffffff") +
        '">' +
        '<root><mxCell id="0"/><mxCell id="1" parent="0"/>' +
        b.cells.join("") +
        "</root></mxGraphModel>";
      return {
        name: name,
        xml:
          '<mxfile host="archify" type="device"><diagram id="archify" name="' +
          esc(name) +
          '">' +
          model +
          "</diagram></mxfile>",
      };
    } finally {
      if (was) root.setAttribute("data-theme", was);
      freeze.remove();
    }
  }

  function slug(text) {
    return (
      String(text)
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "")
        .slice(0, 80) || "diagram"
    );
  }

  function download(theme) {
    var out = build(theme);
    var blob = new Blob([out.xml], { type: "application/vnd.jgraph.mxfile" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = slug(out.name) + (theme === "dark" ? "-dark" : "") + ".drawio";
    document.body.appendChild(a);
    a.click();
    setTimeout(function () {
      a.remove();
      URL.revokeObjectURL(url);
    }, 4000);
    return out;
  }

  function mount() {
    var menu = document.getElementById("export-menu");
    if (!menu || menu.querySelector("[data-drawio]")) return !!menu;
    var section = document.createElement("div");
    section.className = "export-menu-section";
    section.setAttribute("role", "group");
    section.setAttribute("aria-label", "Editable");
    section.innerHTML =
      '<span class="export-menu-heading" aria-hidden="true">Editable</span>' +
      ["light", "dark"]
        .map(function (theme) {
          return (
            '<button data-drawio="' +
            theme +
            '" type="button" role="menuitem" tabindex="-1"><span class="export-item-copy"><strong>draw.io \u00b7 ' +
            (theme === "light" ? "Light" : "Dark") +
            '</strong><small class="hint">Editable diagram, ' +
            theme +
            " colours</small></span></button>"
          );
        })
        .join("");
    menu.appendChild(section);
    section.addEventListener("click", function (e) {
      var btn = e.target.closest("button[data-drawio]");
      if (!btn) return;
      e.stopPropagation();
      try {
        if (window.Archify && Archify.exportMenu)
          Archify.exportMenu.close(true);
      } catch (_) {}
      try {
        download(btn.getAttribute("data-drawio"));
      } catch (err) {
        console.error(err);
        alert(
          "draw.io export failed: " + (err && err.message ? err.message : err),
        );
      }
    });
    return true;
  }

  window.ArchifyDrawio = { build: build, download: download };
  var tries = 0;
  (function wait() {
    if (!mount() && ++tries < 120) setTimeout(wait, 50);
  })();
})();
