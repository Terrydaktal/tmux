package android.util;

// Test-only Android compatibility. Never accesses an Android device.
public final class Base64 {
    public static byte[] decode(String value, int flags) {
        return java.util.Base64.getMimeDecoder().decode(value);
    }
}
