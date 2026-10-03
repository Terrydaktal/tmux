import fs from "node:fs";

const record = (data) => fs.appendFileSync(process.argv[2], `${JSON.stringify(data)}\n`);
const paragraphs = Array.from({ length: 60 }, (_, index) =>
  Array.from({ length: 45 }, (_, word) => `P${index}W${word}`).join(" ")
);
let lastSize = "";
let painting = false;
let again = false;

function render() {
  const width = process.stdout.columns;
  const rows = process.stdout.rows;
  if (painting) { again = true; return; }
  if (lastSize === `${width}x${rows}`) return;
  lastSize = `${width}x${rows}`;
  painting = true;
  if (process.env.REFLOW_SOFT_WRAP && !process.env.REFLOW_INITIAL_PAINT) {
    process.env.REFLOW_INITIAL_PAINT = "1";
    process.stdout.write(paragraphs.join("\r\n") + "\r\nREADY");
    record({ painted: width, rows });
    painting = false;
    return;
  }
  if (process.env.REFLOW_SOFT_WRAP) {
    record({ painted: width, rows });
    painting = false;
    return;
  }
  const lines = [];
  for (const paragraph of paragraphs) {
    let line = "";
    for (const word of paragraph.split(" ")) {
      if (line.length + word.length + 1 > width - 2) {
        lines.push(line);
        line = "";
      }
      line += (line ? " " : "") + word;
    }
    lines.push(line);
  }
  const clear = "\x1b[H\x1b[2J\x1b[3J";
  process.stdout.write(process.env.REFLOW_CLEAR_OUTSIDE_SYNC
    ? clear + "\x1b[?2026h" : "\x1b[?2026h" + clear);
  const split = Math.floor(lines.length / 2);
  process.stdout.write(lines.slice(0, split).join("\r\n") + "\r\n");
  record({ begun: width, rows });
  setTimeout(() => {
    process.stdout.write(lines.slice(split).join("\r\n") + "\r\nREADY\x1b[?2026l", () => {
      record({ painted: width, rows });
      painting = false;
      if (again) { again = false; render(); }
    });
  }, Number(process.env.REFLOW_PAINT_DELAY_MS || "20"));
}

process.stdin.setRawMode(true);
process.stdin.on("data", (data) => record({ input: data.toString("hex") }));
process.stdout.on("resize", render);
render();
