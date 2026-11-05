(function () {
  "use strict";
  // Chat charts (#646): draw each [data-chat-chart] figure from the series the server stored with
  // the turn. Chart.js is loaded only when a chart is on the page; each figure carries its URL.
  var drawn = [];
  var waiting = [];
  var loading = false;
  var probe = null;
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)");

  function themeColor(name) {
    if (!probe) {
      probe = document.createElement("span");
      probe.setAttribute("aria-hidden", "true");
      probe.style.cssText = "position:absolute;visibility:hidden;pointer-events:none";
      document.body.appendChild(probe);
    }
    probe.style.color = "var(" + name + ")";
    return window.getComputedStyle(probe).color;
  }

  // The same theme tokens charts.js reads, so chat charts follow light and dark like the report pages.
  function palette() {
    var categories = [];
    for (var index = 0; index < 8; index += 1) categories.push(themeColor("--chart-category-" + index));
    return {
      text: themeColor("--color-base-content"),
      grid: themeColor("--color-base-300"),
      income: themeColor("--chart-income"),
      spending: themeColor("--chart-spending"),
      primary: themeColor("--color-primary"),
      secondary: themeColor("--color-secondary"),
      categories: categories,
    };
  }

  function axisAmount(value) {
    return (value / 100).toLocaleString(undefined, {maximumFractionDigits: 2});
  }

  function payloadOf(figure) {
    var node = figure.querySelector('script[type="application/json"]');
    if (!node) return null;
    try {
      return JSON.parse(node.textContent);
    } catch (_) {
      return null;
    }
  }

  function seriesColor(series, colors) {
    if (series.color && series.color.indexOf("category-") === 0) {
      return colors.categories[Number(series.color.slice(9)) % 8] || colors.categories[0];
    }
    return colors[series.color] || colors.primary;
  }

  function pointColors(series, colors) {
    return (series.colors || []).map(function (index) {
      return index == null ? colors.categories[0] : colors.categories[index % 8];
    });
  }

  function config(data, colors) {
    var style = data.style;
    var horizontal = style === "bar" && data.kind === "category_breakdown";
    var donut = style === "donut";
    var datasets = data.series.map(function (series) {
      var color = seriesColor(series, colors);
      var fill = series.colors ? pointColors(series, colors) : color;
      return {
        label: series.name,
        data: series.values,
        displays: series.displays,
        backgroundColor: fill,
        borderColor: donut ? colors.grid : color,
        borderWidth: style === "line" ? 2 : (donut ? 1 : 0),
        tension: 0.2,
        pointRadius: 2,
      };
    });
    var options = {
      responsive: true,
      maintainAspectRatio: false,
      animation: reduced.matches ? false : {onComplete: applyDeferredResize},
      plugins: {
        legend: {display: donut || data.series.length > 1, position: "bottom", labels: {color: colors.text, boxWidth: 12}},
        tooltip: {
          callbacks: {
            label: function (context) {
              var display = (context.dataset.displays || [])[context.dataIndex];
              var name = donut ? context.label : context.dataset.label;
              return name + ": " + (display || context.formattedValue);
            },
          },
        },
      },
    };
    if (!donut) {
      var amountAxis = {ticks: {color: colors.text, callback: axisAmount}, grid: {color: colors.grid}};
      var labelAxis = {ticks: {color: colors.text, maxRotation: 45, minRotation: 0, autoSkip: true}, grid: {color: colors.grid}};
      options.indexAxis = horizontal ? "y" : "x";
      options.scales = horizontal ? {x: amountAxis, y: labelAxis} : {x: labelAxis, y: amountAxis};
    }
    return {type: donut ? "doughnut" : style, data: {labels: data.labels, datasets: datasets}, options: options};
  }

  // Chart.js defers a resize that lands mid-animation to the next draw; draw once more when an
  // animation ends so the canvas never overflows its bubble (the charts.js fix for #386).
  function applyDeferredResize(context) {
    if (context.chart._resizeBeforeDraw) context.chart.draw();
  }

  function sizeFor(box, data) {
    if (data.kind === "category_breakdown" && data.style === "bar") {
      box.style.height = Math.max(160, data.labels.length * 28 + 48) + "px";
    }
  }

  function draw(figure, colors) {
    var data = payloadOf(figure);
    var canvas = figure.querySelector("canvas");
    if (!data || !canvas || !Array.isArray(data.series)) return;
    sizeFor(canvas.parentElement, data);
    drawn.push({figure: figure, chart: new window.Chart(canvas, config(data, colors))});
  }

  function drawAll(figures) {
    var colors = palette();
    figures.forEach(function (figure) { draw(figure, colors); });
  }

  function loadLibrary(libraryUrl) {
    if (loading || !libraryUrl) return;
    loading = true;
    var tag = document.createElement("script");
    tag.src = libraryUrl;
    tag.addEventListener("load", function () {
      var pending = waiting.filter(function (figure) { return figure.isConnected; });
      waiting = [];
      drawAll(pending);
    });
    document.head.appendChild(tag);
  }

  function start(root) {
    // Charts whose figure left the page (a replaced popover thread) are released first.
    drawn = drawn.filter(function (item) {
      if (item.figure.isConnected) return true;
      item.chart.destroy();
      return false;
    });
    var figures = Array.from((root || document).querySelectorAll("[data-chat-chart]:not([data-chat-chart-drawn])"));
    figures.forEach(function (figure) { figure.setAttribute("data-chat-chart-drawn", ""); });
    if (!figures.length) return;
    if (window.Chart) {
      drawAll(figures);
    } else {
      waiting = waiting.concat(figures);
      loadLibrary(figures[0].getAttribute("data-chart-lib"));
    }
  }

  function redraw() {
    var figures = drawn.map(function (item) {
      item.chart.destroy();
      return item.figure;
    }).filter(function (figure) { return figure.isConnected; });
    drawn = [];
    if (window.Chart) drawAll(figures);
  }

  window.ChatCharts = {start: start};
  window.addEventListener("financial-planner:themechange", redraw);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { start(document); });
  } else {
    start(document);
  }
})();
