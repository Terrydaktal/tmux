package android.util;

// Error reports stay in the isolated test process, never Android/system logs.
public final class Log {
    public static int e(String tag, String message) { System.err.println(tag + ": " + message); return 0; }
    public static int w(String tag, String message) { return e(tag, message); }
    public static int i(String tag, String message) { return 0; }
    public static int d(String tag, String message) { return 0; }
    public static int v(String tag, String message) { return 0; }
}
