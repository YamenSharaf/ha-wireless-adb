# R8 rules for Wireless ADB Helper.
#
# Activities, receivers and workers are kept through the manifest and
# WorkManager's own consumer rules; OkHttp and kotlinx-coroutines ship theirs.
# Keep class names readable in stack traces from the field.
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile
