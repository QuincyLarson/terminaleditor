class Terminaleditor < Formula
  desc "Tiny modeless terminal text editor with autosave and paragraph movement"
  homepage "https://github.com/quincylarson/terminaleditor"
  url "https://github.com/quincylarson/terminaleditor/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "REPLACE_WITH_RELEASE_TARBALL_SHA256"
  license "MIT"

  depends_on "python@3.14"

  def install
    bin.install "terminaleditor.py" => "terminaleditor"
  end

  test do
    assert_match "terminaleditor 0.1.0",
                 shell_output("#{bin}/terminaleditor --version")
  end
end