/* Sakura Edition — 侧边栏花瓣飘落（独立装饰层，不依赖/不修改任何现有对象）
   特性：pointer-events:none / prefers-reduced-motion 时完全不生成 / 极少 DOM（8片） */
(function () {
  "use strict";
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  ready(function () {
    var sidebar = document.querySelector(".sidebar");
    if (!sidebar || document.getElementById("sakura-petals")) return;

    var layer = document.createElement("div");
    layer.id = "sakura-petals";
    layer.setAttribute("aria-hidden", "true");
    sidebar.appendChild(layer);

    var PETALS = 8;
    for (var i = 0; i < PETALS; i++) {
      var p = document.createElement("span");
      p.className = "petal";
      // 随机但确定性的分布：起始位置 / 延迟 / 时长 / 大小 / 旋转
      var seed = i / PETALS;
      p.style.left = (6 + seed * 82 + Math.sin(i * 2.7) * 4) + "%";
      p.style.animationDelay = (-seed * 16).toFixed(1) + "s";
      p.style.animationDuration = (14 + (i % 4) * 3) + "s";
      var size = 7 + (i % 3) * 2;
      p.style.width = size + "px";
      p.style.height = size + "px";
      p.style.setProperty("--sway", (18 + (i % 5) * 6) + "px");
      p.style.setProperty("--spin", (i % 2 ? 1 : -1) * (160 + i * 30) + "deg");
      layer.appendChild(p);
    }
  });
})();
