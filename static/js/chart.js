function drawChart() {
    const canvas = document.getElementById("chart");
    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    const series = [
        { data: state.history.greedy, color: "#d45d5d" },
        { data: state.history.smart, color: "#3db8a0" },
    ];
    const all = series.flatMap((s) => s.data);
    if (!all.length) return;
    const maxT = Math.max(...all.map((p) => p.t), 1);
    const maxY = Math.max(...all.map((p) => p.y), 1);
    series.forEach((s) => {
        if (!s.data.length) return;
        ctx.beginPath();
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 2;
        s.data.forEach((p, i) => {
            const x = (p.t / maxT) * (w - 8) + 4;
            const y = h - 6 - (p.y / maxY) * (h - 14);
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
        ctx.stroke();
    });
}
