package com.otodock;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;

/** Reads the app's sources for the source-level tests. Gradle runs unit tests
 *  with the module directory (dashboard/android/app) as the working directory. */
final class SourceText {

    private SourceText() {}

    static String read(String path) throws IOException {
        return new String(Files.readAllBytes(Paths.get(path)), StandardCharsets.UTF_8);
    }

    static String javaSource(String className) throws IOException {
        return withoutComments(read("src/main/java/com/otodock/" + className + ".java"));
    }

    /** Drops // and block comments, leaving string and char literals intact
     *  ("file:///android_asset" must survive). */
    static String withoutComments(String src) {
        StringBuilder out = new StringBuilder(src.length());
        int i = 0;
        int n = src.length();
        while (i < n) {
            char c = src.charAt(i);
            if (c == '"' || c == '\'') {
                int j = i + 1;
                while (j < n && src.charAt(j) != c) {
                    if (src.charAt(j) == '\\') j++;
                    j++;
                }
                out.append(src, i, Math.min(j + 1, n));
                i = j + 1;
            } else if (c == '/' && i + 1 < n && src.charAt(i + 1) == '/') {
                while (i < n && src.charAt(i) != '\n') i++;
            } else if (c == '/' && i + 1 < n && src.charAt(i + 1) == '*') {
                int end = src.indexOf("*/", i + 2);
                i = end < 0 ? n : end + 2;
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
    }

    /** The text between the brace that opens after {@code from} and its match. */
    static String blockAfter(String src, int from) {
        int open = src.indexOf('{', from);
        if (open < 0) throw new AssertionError("no block after offset " + from);
        int depth = 0;
        for (int i = open; i < src.length(); i++) {
            char c = src.charAt(i);
            if (c == '"' || c == '\'') {
                int j = i + 1;
                while (j < src.length() && src.charAt(j) != c) {
                    if (src.charAt(j) == '\\') j++;
                    j++;
                }
                i = j;
                continue;
            }
            if (c == '{') depth++;
            else if (c == '}' && --depth == 0) return src.substring(open + 1, i);
        }
        throw new AssertionError("unbalanced block after offset " + from);
    }

    /** The body of the first method or class whose declaration matches {@code signature}. */
    static String body(String src, String signature) {
        int at = src.indexOf(signature);
        if (at < 0) throw new AssertionError("not found: " + signature);
        return blockAfter(src, at);
    }

    static int count(String haystack, String needle) {
        int n = 0;
        for (int i = haystack.indexOf(needle); i >= 0; i = haystack.indexOf(needle, i + needle.length())) n++;
        return n;
    }
}
