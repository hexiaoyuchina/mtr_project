// GoBGP架构状态管理
(function() {
  'use strict';

  let gobgpStatusTimer = null;

  // 更新GoBGP状态面板
  async function updateGoBGPStatus() {
    try {
      const status = await fetch('/api/gobgp/status').then(r => r.json());
      
      // RR连接状态
      const rrStatus = document.getElementById('gobgpRRStatus');
      if (rrStatus) {
        const connected = status.rr?.rr_connected;
        const state = status.rr?.rr_state || 'Unknown';
        if (connected) {
          rrStatus.innerHTML = `<span style="color: var(--good);">✓ 已连接 (${state})</span>`;
        } else {
          rrStatus.innerHTML = `<span style="color: var(--bad);">✗ 断开 (${state})</span>`;
        }
      }

      // Freeze状态
      const freezeStatus = document.getElementById('gobgpFreezeStatus');
      if (freezeStatus) {
        const frozen = status.rr?.frozen || status.agent?.processor?.frozen;
        if (frozen) {
          freezeStatus.innerHTML = `<span style="color: var(--warn);">❄️ 冻结（保持RIB）</span>`;
        } else {
          freezeStatus.innerHTML = `<span style="color: var(--good);">✓ 运行中</span>`;
        }
      }

      // 路由数量
      const routeCount = document.getElementById('gobgpRouteCount');
      if (routeCount) {
        const count = status.agent?.processor?.route_count || 0;
        routeCount.textContent = count.toLocaleString();
      }

    } catch (e) {
      console.error('更新GoBGP状态失败:', e);
      const rrStatus = document.getElementById('gobgpRRStatus');
      if (rrStatus) {
        rrStatus.innerHTML = `<span style="color: var(--muted);">Agent未运行</span>`;
      }
    }
  }

  // 显示详细状态
  async function showDetailedStatus() {
    try {
      const status = await fetch('/api/gobgp/status').then(r => r.json());
      alert(JSON.stringify(status, null, 2));
    } catch (e) {
      alert('获取状态失败: ' + e.message);
    }
  }

  // 冻结系统（测试RR down）
  async function freezeSystem() {
    if (!confirm('确定要冻结BGP系统吗？\n\n这将模拟RR断连场景：\n- 停止接受新路由更新\n- 保持当前RIB继续通告\n- 适用于测试HA场景')) {
      return;
    }

    try {
      const result = await fetch('/api/gobgp/freeze', { method: 'POST' }).then(r => r.json());
      alert('系统已冻结\n\n' + (result.message || '保持当前RIB，继续通告'));
      await updateGoBGPStatus();
    } catch (e) {
      alert('冻结失败: ' + e.message);
    }
  }

  // 解冻系统
  async function unfreezeSystem() {
    if (!confirm('确定要解冻BGP系统吗？\n\n这将恢复正常运行：\n- 开始接受新路由更新\n- 允许路由撤销')) {
      return;
    }

    try {
      const result = await fetch('/api/gobgp/unfreeze', { method: 'POST' }).then(r => r.json());
      alert('系统已解冻\n\n' + (result.message || '恢复接受路由更新'));
      await updateGoBGPStatus();
    } catch (e) {
      alert('解冻失败: ' + e.message);
    }
  }

  // 启动定时刷新
  function startGoBGPStatusPoll() {
    if (gobgpStatusTimer) return;
    updateGoBGPStatus();
    gobgpStatusTimer = setInterval(updateGoBGPStatus, 10000); // 每10秒刷新
  }

  // 停止定时刷新
  function stopGoBGPStatusPoll() {
    if (gobgpStatusTimer) {
      clearInterval(gobgpStatusTimer);
      gobgpStatusTimer = null;
    }
  }

  // 页面切换时处理
  function handlePageChange(pageName) {
    if (pageName === 'bgp') {
      startGoBGPStatusPoll();
    } else {
      stopGoBGPStatusPoll();
    }
  }

  // 初始化按钮事件
  document.addEventListener('DOMContentLoaded', function() {
    const btnStatus = document.getElementById('btnGoBGPStatus');
    if (btnStatus) {
      btnStatus.addEventListener('click', showDetailedStatus);
    }

    const btnFreeze = document.getElementById('btnGoBGPFreeze');
    if (btnFreeze) {
      btnFreeze.addEventListener('click', freezeSystem);
    }

    const btnUnfreeze = document.getElementById('btnGoBGPUnfreeze');
    if (btnUnfreeze) {
      btnUnfreeze.addEventListener('click', unfreezeSystem);
    }

    // 监听页面切换
    const observer = new MutationObserver(function(mutations) {
      mutations.forEach(function(mutation) {
        if (mutation.attributeName === 'class') {
          const target = mutation.target;
          if (target.classList.contains('show')) {
            const pageName = target.getAttribute('data-page');
            handlePageChange(pageName);
          }
        }
      });
    });

    document.querySelectorAll('.page').forEach(function(page) {
      observer.observe(page, { attributes: true });
    });

    // 如果当前在BGP页面，立即启动
    const currentPage = document.querySelector('.page.show');
    if (currentPage && currentPage.getAttribute('data-page') === 'bgp') {
      startGoBGPStatusPoll();
    }
  });

  // 导出到全局
  window.GoBGPStatus = {
    update: updateGoBGPStatus,
    start: startGoBGPStatusPoll,
    stop: stopGoBGPStatusPoll
  };
})();
