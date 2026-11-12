import btools.mapaccess.PhysicalFile;
import java.io.File;

class VerifyMapUpdateRd5 {
    public static void main(String[] args) throws Exception {
        File segment = new File(args[0]);
        String error = PhysicalFile.checkFileIntegrity(segment);
        if (error != null) {
            throw new IllegalArgumentException(segment.getName() + ": " + error);
        }
        System.out.println(segment.getName() + " passed BRouter integrity check");
    }
}
