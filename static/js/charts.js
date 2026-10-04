(function () {
  var charts = [];

  function payloadFrom(id) {
    var node = document.getElementById(id);
    if (!node) {
      return null;
    }
    return JSON.parse(node.textContent);
  }

  function cssVarColor(name) {
    var probe = cssVarColor.probe;
    if (!probe) {
      probe = document.createElement("span");
      probe.setAttribute("aria-hidden", "true");
      probe.style.position = "absolute";
      probe.style.pointerEvents = "none";
      probe.style.visibility = "hidden";
      document.body.appendChild(probe);
      cssVarColor.probe = probe;
    }
    probe.style.color = "var(" + name + ")";
    return window.getComputedStyle(probe).color;
  }

  function themePalette() {
    var categories = [];
    var index;
    for (index = 0; index < 8; index += 1) {
      categories.push(cssVarColor("--chart-category-" + index));
    }
    return {
      text: cssVarColor("--color-base-content"),
      grid: cssVarColor("--color-base-300"),
      income: cssVarColor("--color-success"),
      spending: cssVarColor("--color-error"),
      net: cssVarColor("--color-primary"),
      warning: cssVarColor("--color-warning"),
      categories: categories,
    };
  }

  // Chart data is in minor units (cents) so values stay exact; axis labels show
  // the same currency amounts as the tooltips and tables.
  function formatAxisMinor(value) {
    return (value / 100).toLocaleString(undefined, { maximumFractionDigits: 2 });
  }

  function goTo(url) {
    if (url) {
      window.location.assign(url);
    }
  }

  var missingImportMarker = {
    id: "missingImportMarker",
    afterDatasetsDraw: function (chart) {
      var flags = (chart.options.plugins.financialPlanner || {}).missingImport || [];
      var meta = chart.getDatasetMeta(0);
      var color = themePalette().warning;
      var ctx = chart.ctx;
      flags.forEach(function (missing, index) {
        var point = meta.data[index];
        if (!missing || !point) {
          return;
        }
        var x = point.x;
        var y = chart.chartArea.top + 8;
        ctx.save();
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.moveTo(x, y);
        ctx.lineTo(x - 6, y + 10);
        ctx.lineTo(x + 6, y + 10);
        ctx.closePath();
        ctx.fill();
        ctx.restore();
      });
    },
  };

  // Theme colors arrive in whatever syntax the browser computes (rgb() or
  // oklch() for daisyUI themes). Paint one pixel to read them back as RGB, so
  // any CSS color can take a new alpha.
  function withAlpha(color, alpha) {
    var probe = document.createElement("canvas");
    probe.width = 1;
    probe.height = 1;
    var context = probe.getContext("2d", { willReadFrequently: true });
    if (!context) {
      return color;
    }
    context.fillStyle = color;
    context.fillRect(0, 0, 1, 1);
    var pixel = context.getImageData(0, 0, 1, 1).data;
    return "rgba(" + pixel[0] + ", " + pixel[1] + ", " + pixel[2] + ", " + alpha + ")";
  }

  function cashFlowChart(canvas, data, palette) {
    var periods = data.periods || [];
    var incomeDisplays = periods.map(function (row) {
      return row.income_display;
    });
    var spendingDisplays = periods.map(function (row) {
      return row.spending_display;
    });
    var netDisplays = periods.map(function (row) {
      return row.net_display;
    });
    function barColor(base) {
      return periods.map(function (row) {
        if (row.projected) {
          return withAlpha(base, 0.35);
        }
        return base;
      });
    }
    return new window.Chart(canvas, {
      data: {
        labels: periods.map(function (row) {
          return row.label;
        }),
        datasets: [
          {
            type: "bar",
            label: "Income",
            data: periods.map(function (row) {
              return row.income_minor;
            }),
            backgroundColor: barColor(palette.income),
            borderColor: palette.income,
            borderWidth: periods.map(function (row) {
              return row.projected ? 1 : 0;
            }),
            borderDash: [6, 4],
            displays: incomeDisplays,
          },
          {
            type: "bar",
            label: "Spending",
            data: periods.map(function (row) {
              return row.spending_minor;
            }),
            backgroundColor: barColor(palette.spending),
            borderColor: palette.spending,
            borderWidth: periods.map(function (row) {
              return row.projected ? 1 : 0;
            }),
            borderDash: [6, 4],
            displays: spendingDisplays,
          },
            {
            type: "line",
            label: "Baseline net",
            data: periods.map(function (row) {
              return row.net_minor;
            }),
            borderColor: palette.net,
            backgroundColor: palette.net,
            tension: 0.2,
            displays: netDisplays,
            segment: {
              borderDash: function (ctx) {
                var row = periods[ctx.p1DataIndex];
                return row && row.projected ? [6, 4] : [];
              },
            },
          },
          {
            type: "line",
            label: "Scenario net",
            data: periods.map(function (row) {
              if (row.scenario_net_minor == null) {
                return null;
              }
              return row.scenario_net_minor;
            }),
            borderColor: palette.warning,
            backgroundColor: palette.warning,
            tension: 0.2,
            displays: periods.map(function (row) {
              return row.scenario_net_display;
            }),
            segment: {
              borderDash: function (ctx) {
                var row = periods[ctx.p1DataIndex];
                return row && row.projected ? [6, 4] : [];
              },
            },
          },
          {
            type: "line",
            label: "Projected",
            data: periods.map(function () {
              return null;
            }),
            borderColor: palette.net,
            backgroundColor: "transparent",
            borderDash: [6, 4],
            pointRadius: 0,
          },
        ],
      },
      plugins: [missingImportMarker],
      options: {
        responsive: true,
        maintainAspectRatio: false,
        onClick: function (event, elements) {
          if (elements.length) {
            goTo(periods[elements[0].index].drilldown_url);
          }
        },
        onHover: function (event, elements) {
          var native = event.native || event;
          if (native && native.target) {
            native.target.style.cursor = elements.length ? "pointer" : "default";
          }
        },
        plugins: {
          legend: { labels: { color: palette.text } },
          tooltip: {
            callbacks: {
              label: function (context) {
                var displays = context.dataset.displays || [];
                var display = displays[context.dataIndex];
                if (display) {
                  return context.dataset.label + ": " + display;
                }
                return context.dataset.label + ": " + context.formattedValue;
              },
            },
          },
          financialPlanner: {
            missingImport: periods.map(function (row) {
              return row.missing_import;
            }),
          },
        },
        scales: {
          x: {
            ticks: { color: palette.text, maxRotation: 45, minRotation: 0 },
            grid: { color: palette.grid },
          },
          y: {
            ticks: { color: palette.text, callback: formatAxisMinor },
            title: { display: true, text: "USD", color: palette.text },
            grid: { color: palette.grid },
          },
        },
      },
    });
  }

  function debtPayoffChart(canvas, data, palette) {
    var labels = data.labels || [];
    return new window.Chart(canvas, {
      data: {
        labels: labels,
        datasets: [
          {
            type: "bar",
            label: "Interest this month",
            data: data.interest_minor || [],
            backgroundColor: palette.spending,
            displays: data.interest_display || [],
          },
          {
            type: "line",
            label: "Remaining",
            data: data.remaining_minor || [],
            borderColor: palette.net,
            backgroundColor: palette.net,
            tension: 0.2,
            displays: data.remaining_display || [],
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: palette.text } },
          tooltip: {
            callbacks: {
              label: function (context) {
                var displays = context.dataset.displays || [];
                var display = displays[context.dataIndex];
                if (display) {
                  return context.dataset.label + ": " + display;
                }
                return context.dataset.label + ": " + context.formattedValue;
              },
            },
          },
        },
        scales: {
          x: {
            ticks: { color: palette.text, maxRotation: 45, minRotation: 0 },
            grid: { color: palette.grid },
          },
          y: {
            ticks: { color: palette.text, callback: formatAxisMinor },
            title: { display: true, text: "USD", color: palette.text },
            grid: { color: palette.grid },
          },
        },
      },
    });
  }

  function netWorthChart(canvas, data, palette) {
    var periods = data.periods || [];
    return new window.Chart(canvas, {
      data: {
        labels: periods.map(function (row) {
          return row.label;
        }),
        datasets: [
          {
            type: "bar",
            label: "Assets",
            data: periods.map(function (row) {
              return row.assets_minor;
            }),
            backgroundColor: palette.income,
            stack: "balances",
            displays: periods.map(function (row) {
              return row.assets_display;
            }),
          },
          {
            type: "bar",
            label: "Liabilities",
            data: periods.map(function (row) {
              return -row.liabilities_minor;
            }),
            backgroundColor: palette.spending,
            stack: "balances",
            displays: periods.map(function (row) {
              return row.liabilities_display;
            }),
          },
          {
            type: "line",
            label: "Net worth",
            data: periods.map(function (row) {
              return row.net_minor;
            }),
            borderColor: palette.net,
            backgroundColor: palette.net,
            tension: 0.2,
            displays: periods.map(function (row) {
              return row.net_display;
            }),
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: palette.text } },
          tooltip: {
            callbacks: {
              label: function (context) {
                var displays = context.dataset.displays || [];
                var display = displays[context.dataIndex];
                if (display) {
                  return context.dataset.label + ": " + display;
                }
                return context.dataset.label + ": " + context.formattedValue;
              },
            },
          },
        },
        scales: {
          x: {
            ticks: { color: palette.text, maxRotation: 45, minRotation: 0 },
            grid: { color: palette.grid },
          },
          y: {
            ticks: { color: palette.text, callback: formatAxisMinor },
            title: { display: true, text: "USD", color: palette.text },
            grid: { color: palette.grid },
          },
        },
      },
    });
  }

  function spendingChart(canvas, data, palette) {
    // Only positive spending can be drawn as a slice; net-refund categories
    // stay in the tiles and table (see chart_rows in spending_chart_data).
    var rows = data.chart_rows || [];
    var urls = rows.map(function (row) {
      return row.drilldown_url;
    });
    return new window.Chart(canvas, {
      type: "doughnut",
      data: {
        labels: rows.map(function (row) {
          return row.name;
        }),
        datasets: [
          {
            label: "Spending",
            data: rows.map(function (row) {
              return row.spending_minor;
            }),
            backgroundColor: rows.map(function (row) {
              return palette.categories[row.color_index] || palette.categories[0];
            }),
            displays: rows.map(function (row) {
              return row.spending_display + " (" + row.share_display + " of charted spending)";
            }),
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        onClick: function (event, elements) {
          if (elements.length) {
            goTo(urls[elements[0].index]);
          }
        },
        onHover: function (event, elements) {
          var native = event.native || event;
          if (native && native.target) {
            native.target.style.cursor = elements.length ? "pointer" : "default";
          }
        },
        plugins: {
          legend: { position: "bottom", labels: { color: palette.text } },
          tooltip: {
            callbacks: {
              label: function (context) {
                var displays = context.dataset.displays || [];
                var display = displays[context.dataIndex];
                if (display) {
                  return context.label + ": " + display;
                }
                return context.label + ": " + context.formattedValue;
              },
            },
          },
        },
      },
    });
  }

  function spendingTrendChart(canvas, data, palette) {
    var periods = data.periods || [];
    var series = data.chart_series || [];
    return new window.Chart(canvas, {
      type: "bar",
      data: {
        labels: periods.map(function (row) {
          return row.label;
        }),
        datasets: series.map(function (item) {
          return {
            label: item.name,
            data: item.values,
            backgroundColor: palette.categories[item.color_index] || palette.categories[0],
            stack: "spending",
            displays: item.displays,
            detailUrl: item.detail_url,
          };
        }),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        onClick: function (event, elements) {
          if (!elements.length) {
            return;
          }
          var dataset = series[elements[0].datasetIndex];
          if (dataset && dataset.detail_url) {
            goTo(dataset.detail_url);
          }
        },
        onHover: function (event, elements) {
          var native = event.native || event;
          if (native && native.target) {
            native.target.style.cursor = elements.length ? "pointer" : "default";
          }
        },
        plugins: {
          legend: { position: "bottom", labels: { color: palette.text } },
          tooltip: {
            callbacks: {
              label: function (context) {
                var displays = context.dataset.displays || [];
                var display = displays[context.dataIndex];
                if (display) {
                  return context.dataset.label + ": " + display;
                }
                return context.dataset.label + ": " + context.formattedValue;
              },
            },
          },
        },
        scales: {
          x: {
            stacked: true,
            ticks: { color: palette.text, maxRotation: 45, minRotation: 0 },
            grid: { color: palette.grid },
          },
          y: {
            stacked: true,
            ticks: { color: palette.text, callback: formatAxisMinor },
            title: { display: true, text: "USD", color: palette.text },
            grid: { color: palette.grid },
          },
        },
      },
    });
  }

  function categoryTrendChart(canvas, data, palette) {
    var periods = data.periods || [];
    return new window.Chart(canvas, {
      type: "bar",
      data: {
        labels: periods.map(function (row) {
          return row.label;
        }),
        datasets: [
          {
            label: data.name,
            data: periods.map(function (row) {
              return row.spending_minor;
            }),
            backgroundColor: palette.categories[data.color_index] || palette.categories[0],
            displays: periods.map(function (row) {
              return row.spending_display;
            }),
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function (context) {
                var displays = context.dataset.displays || [];
                var display = displays[context.dataIndex];
                if (display) {
                  return context.dataset.label + ": " + display;
                }
                return context.dataset.label + ": " + context.formattedValue;
              },
            },
          },
        },
        scales: {
          x: {
            ticks: { color: palette.text, maxRotation: 45, minRotation: 0 },
            grid: { color: palette.grid },
          },
          y: {
            ticks: { color: palette.text, callback: formatAxisMinor },
            title: { display: true, text: "USD", color: palette.text },
            grid: { color: palette.grid },
          },
        },
      },
    });
  }

  function destroyCharts() {
    charts.forEach(function (chart) {
      chart.destroy();
    });
    charts = [];
  }

  function renderCharts() {
    if (!window.Chart) {
      return;
    }
    destroyCharts();
    var palette = themePalette();
    window.Chart.defaults.color = palette.text;
    window.Chart.defaults.borderColor = palette.grid;
    document.querySelectorAll("canvas[data-chart]").forEach(function (canvas) {
      var data = payloadFrom(canvas.getAttribute("data-chart-payload"));
      var kind = canvas.getAttribute("data-chart");
      if (!data) {
        return;
      }
      if (kind === "cash-flow") {
        charts.push(cashFlowChart(canvas, data, palette));
      } else if (kind === "spending") {
        charts.push(spendingChart(canvas, data, palette));
      } else if (kind === "spending-trend") {
        charts.push(spendingTrendChart(canvas, data, palette));
      } else if (kind === "category-trend") {
        charts.push(categoryTrendChart(canvas, data, palette));
      } else if (kind === "net-worth") {
        charts.push(netWorthChart(canvas, data, palette));
      } else if (kind === "debt-payoff") {
        charts.push(debtPayoffChart(canvas, data, palette));
      }
    });
  }

  document.addEventListener("DOMContentLoaded", renderCharts);
  window.addEventListener("financial-planner:themechange", renderCharts);
})();
