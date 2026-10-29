import fs from "node:fs";

const { ProcessTerminal, Text, TuiMainScreen } = await import(process.env.PI_TUI_MODULE);
const log = process.argv[2];
const record = (data) => fs.appendFileSync(log, `${JSON.stringify(data)}\n`);
const paragraphs = Array.from({ length: 30 }, (_, index) =>
  `MESSAGE-${String(index).padStart(2, "0")} ` +
  Array.from({ length: 35 }, (_, word) => `P${index}W${word}` +
    (process.env.PI_TEST_UNICODE ? "\u7ea2\u9b54\u03b4e\u0301" : "")).join(" ")
).join("\n") + "\nREADY";

const terminal = new ProcessTerminal();
const write = terminal.write.bind(terminal);
terminal.write = (data) => {
  if (data === "\x1b[5n" && process.env.PI_TEST_ACK_DELAY_MS) {
    setTimeout(() => write(data), Number(process.env.PI_TEST_ACK_DELAY_MS));
  } else write(data);
};
const tui = new TuiMainScreen(terminal);
if (process.env.PI_TEST_NO_HISTORY_HELPER) tui.beforeTerminalStart = () => {};
tui.addChild(new Text(paragraphs, 0, 0));
const render = tui.doRender.bind(tui);
tui.doRender = () => {
  render();
  record({ width: terminal.columns, rows: terminal.rows, painted: tui.previousWidth });
};
tui.start();
tui.addInputListener((data) => {
  record({ input: data });
  return { consume: true };
});
setInterval(() => {}, 1000);
