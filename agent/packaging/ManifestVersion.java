// SPDX-License-Identifier: AGPL-3.0-only
// Build-only adapter over Android's official generated Resources.proto API.
// AAPT2 owns both binary XML conversions; no binary resource parser lives here.
import com.android.aapt.Resources.XmlAttribute;
import com.android.aapt.Resources.XmlNode;
import java.nio.file.Files;
import java.nio.file.Path;

class ManifestVersion {
    private static XmlNode version(XmlNode node, String oldVersion, String newVersion) {
        if (!node.hasElement() || !node.getElement().getName().equals("manifest"))
            throw new IllegalArgumentException("Expected one manifest root");
        var result = node.toBuilder();
        int matches = 0;
        for (int i = 0; i < node.getElement().getAttributeCount(); i++) {
            XmlAttribute value = node.getElement().getAttribute(i);
            if (!value.getNamespaceUri().equals("http://schemas.android.com/apk/res/android") ||
                    !value.getName().equals("versionName")) continue;
            if (value.getResourceId() != 0x0101021c || !value.getValue().equals(oldVersion))
                throw new IllegalArgumentException("Producer versionName differs");
            var replacement = value.toBuilder().setValue(newVersion);
            if (value.hasCompiledItem()) {
                if (!value.getCompiledItem().hasStr() ||
                        !value.getCompiledItem().getStr().getValue().equals(oldVersion))
                    throw new IllegalArgumentException("Unsupported compiled versionName");
                replacement.getCompiledItemBuilder().getStrBuilder().setValue(newVersion);
            }
            result.getElementBuilder().setAttribute(i, replacement);
            matches++;
        }
        if (matches != 1) throw new IllegalArgumentException("Expected one versionName attribute");
        return result.build();
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 5 || !(args[0].equals("update") || args[0].equals("verify")))
            throw new IllegalArgumentException("Use update|verify input output oldVersion newVersion");
        if (!args[3].matches("[0-9]+\\.[0-9]+(?:\\.[0-9]+)?") ||
                !args[4].matches("[0-9]+\\.[0-9]+(?:\\.[0-9]+)?"))
            throw new IllegalArgumentException("Invalid product version");
        XmlNode original = XmlNode.parseFrom(Files.readAllBytes(Path.of(args[1])));
        if (args[0].equals("update")) {
            Files.write(Path.of(args[2]), version(original, args[3], args[4]).toByteArray());
        } else {
            XmlNode changed = XmlNode.parseFrom(Files.readAllBytes(Path.of(args[2])));
            if (!version(changed, args[4], args[3]).equals(original))
                throw new IllegalArgumentException("Native manifest conversion changed other XML semantics");
        }
    }
}
