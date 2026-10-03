package com.termux.terminal;

import java.io.*;
import java.lang.reflect.Proxy;
import java.nio.charset.StandardCharsets;
import java.util.Base64;

public final class Oracle {
    public static void main(String[] args) throws Exception {
        ByteArrayOutputStream replies = new ByteArrayOutputStream();
        String[] clipboard = {""};
        TerminalOutput output = new TerminalOutput() {
            public void write(byte[] data, int offset, int count) { replies.write(data, offset, count); }
            public void titleChanged(String oldTitle, String newTitle) {}
            public void onCopyTextToClipboard(String text) { clipboard[0] = text; }
            public void onPasteTextFromClipboard() {}
            public void onBell() {}
            public void onColorsChanged() {}
        };
        TerminalSessionClient client = (TerminalSessionClient) Proxy.newProxyInstance(
            TerminalSessionClient.class.getClassLoader(), new Class<?>[]{TerminalSessionClient.class},
            (proxy, method, values) -> method.getReturnType() == Integer.class ? Integer.valueOf(0) : null);
        int cols = Integer.parseInt(args[0]), rows = Integer.parseInt(args[1]);
        TerminalEmulator term = new TerminalEmulator(output, cols, rows, 8, 16, 10000, client);
        BufferedReader input = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        for (String line; (line = input.readLine()) != null;) {
            String[] fields = line.split(" ", -1);
            if (fields[0].equals("FEED")) {
                byte[] bytes = Base64.getDecoder().decode(fields[1]);
                term.append(bytes, bytes.length);
            } else if (fields[0].equals("SIZE")) {
                cols = Integer.parseInt(fields[1]); rows = Integer.parseInt(fields[2]);
                term.resize(cols, rows, 8, 16);
            }
            String text = term.getScreen().getTranscriptTextWithoutJoinedLines();
            String screen = term.getSelectedText(0, 0, cols - 1, rows - 1);
            StringBuilder physical = new StringBuilder("[");
            for (int y = 0; y < rows; y++) {
                if (y > 0) physical.append(',');
                physical.append('"').append(encode(term.getScreen().getSelectedText(0, y, cols - 1, y, false, false)
                    .getBytes(StandardCharsets.UTF_8))).append('"');
            }
            physical.append(']');
            System.out.println("{\"text\":\"" + encode(text.getBytes(StandardCharsets.UTF_8)) +
                "\",\"screen\":\"" + encode(screen.getBytes(StandardCharsets.UTF_8)) +
                "\",\"replies\":\"" + encode(replies.toByteArray()) + "\",\"mouse\":" + term.isMouseTrackingActive() +
                ",\"alternate\":" + term.isAlternateBufferActive() + ",\"history\":" + term.getScreen().getActiveTranscriptRows() +
                ",\"clipboard\":\"" + encode(clipboard[0].getBytes(StandardCharsets.UTF_8)) + "\"" +
                ",\"lines\":" + physical +
                ",\"x\":" + term.getCursorCol() + ",\"y\":" + term.getCursorRow() + "}");
            System.out.flush(); replies.reset();
        }
    }
    static String encode(byte[] value) { return Base64.getEncoder().encodeToString(value); }
}
