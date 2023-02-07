"""
Github repository reader.

Retrieves the contents of a Github repository and returns a list of documents.
The documents are either the contents of the files in the repository or 
the text extracted from the files using the parser.
"""

import base64
import binascii
import logging
import os
import pathlib
import tempfile
from typing import Any, Callable, List, Optional, Tuple

from gpt_index.readers.base import BaseReader
from gpt_index.readers.file.base import DEFAULT_FILE_EXTRACTOR
from gpt_index.readers.github_readers.github_api_client import (
    GitBranchResponseModel,
    GitCommitResponseModel,
    GithubClient,
    GitTreeResponseModel,
)
from gpt_index.readers.github_readers.utils import (
    BufferedGitBlobDataIterator,
    get_file_extension,
    print_if_verbose,
)
from gpt_index.readers.schema.base import Document

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class GithubRepositoryReader(BaseReader):
    """
    Github repository reader.

    Retrieves the contents of a Github repository and returns a list of documents.
    The documents are either the contents of the files in the repository or the text
    extracted from the files using the parser.

    Examples:
        >>> reader = GithubRepositoryReader("owner", "repo")
        >>> branch_documents = reader.load_data(branch="branch")
        >>> commit_documents = reader.load_data(commit_sha="commit_sha")

    """

    def __init__(
        self,
        owner: str,
        repo: str,
        use_parser: bool = True,
        verbose: bool = False,
        github_token: Optional[str] = None,
    ):
        """
        Initialize params.

        Args:
            - owner (str): Owner of the repository.
            - repo (str): Name of the repository.
            - use_parser (bool): Whether to use the parser to extract the text from the files.
            - verbose (bool): Whether to print verbose messages.
            - github_token (str): Github token. If not provided, it will be read from the GITHUB_TOKEN environment variable.

        Raises:
            - `ValueError`: If the github_token is not provided and the GITHUB_TOKEN environment variable is not set.
        """
        super().__init__(verbose)
        if github_token is None:
            github_token = os.getenv("GITHUB_TOKEN")
            if github_token is None:
                raise ValueError(
                    "Please provide a Github token. "
                    "You can do so by passing it as an argument or"
                    + "by setting the GITHUB_TOKEN environment variable."
                )

        self._owner = owner
        self._repo = repo
        self._use_parser = use_parser
        self._verbose = verbose

        self._client = GithubClient(github_token)

    def _load_data_from_commit(self, commit_sha: str) -> List[Document]:
        """
        Load data from a commit.

        Loads github repository data from a specific commit sha.

        :param `commit`: commit sha

        :return: list of documents
        """

        commit_response: GitCommitResponseModel = self._client.get_commit(
            self._owner, self._repo, commit_sha
        )

        tree_sha = commit_response.tree.sha
        blobs_and_paths = self._recurse_tree(tree_sha)

        print_if_verbose(self._verbose, f"got {len(blobs_and_paths)} blobs")

        return self._generate_documents(blobs_and_paths=blobs_and_paths)

    def _load_data_from_branch(self, branch: str) -> List[Document]:
        """
        Load data from a branch.

        Loads github repository data from a specific branch.

        :param `branch`: branch name

        :return: list of documents
        """
        branch_data: GitBranchResponseModel = self._client.get_branch(
            self._owner, self._repo, branch
        )

        tree_sha = branch_data.commit.commit.tree.sha
        blobs_and_paths = self._recurse_tree(tree_sha)

        print_if_verbose(self._verbose, f"got {len(blobs_and_paths)} blobs")

        return self._generate_documents(blobs_and_paths=blobs_and_paths)

    def load_data(
        self,
        commit_sha: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> List[Document]:
        """
        Load data from a commit or a branch.

        Loads github repository data from a specific commit sha or a branch.

        :param `commit`: commit sha
        :param `branch`: branch name

        :return: list of documents
        """
        if commit_sha is not None and branch is not None:
            raise ValueError("You can only specify one of commit or branch.")

        if commit_sha is None and branch is None:
            raise ValueError("You must specify one of commit or branch.")

        if commit_sha is not None:
            return self._load_data_from_commit(commit_sha)

        if branch is not None:
            return self._load_data_from_branch(branch)

        raise ValueError("You must specify one of commit or branch.")

    def _recurse_tree(
        self, tree_sha: str, current_path: str = "", current_depth: int = 0
    ) -> List[Tuple[GitTreeResponseModel.GitTreeObject, str]]:
        """
        Recursively get all blob tree objects in a tree.
        And construct their full path relative to the root of the repository.
        (see GitTreeResponseModel.GitTreeObject in github_api_client.py for more information)


        :param `tree_sha`: sha of the tree to recurse
        :param `current_path`: current path of the tree
        :param `current_depth`: current depth of the tree
        :return: list of tuples of (tree object, file's full path in the repo realtive to the root of the repo)

        """
        blobs_and_full_paths: List[Tuple[GitTreeResponseModel.GitTreeObject, str]] = []
        print_if_verbose(
            self._verbose, "\t" * current_depth + f"current path: {current_path}"
        )

        tree_data: GitTreeResponseModel = self._client.get_tree(
            self._owner, self._repo, tree_sha
        )
        print_if_verbose(
            self._verbose, "\t" * current_depth + f"processing tree {tree_sha}"
        )
        for tree in tree_data.tree:
            file_path = os.path.join(current_path, tree.path)
            if tree.type == "tree":
                print_if_verbose(
                    self._verbose, "\t" * current_depth + f"recursing into {tree.path}"
                )
                blobs_and_full_paths.extend(
                    self._recurse_tree(tree.sha, file_path, current_depth + 1)
                )
            elif tree.type == "blob":
                print_if_verbose(
                    self._verbose, "\t" * current_depth + f"found blob {tree.path}"
                )
                blobs_and_full_paths.append((tree, file_path))
        return blobs_and_full_paths

    def _generate_documents(
        self, blobs_and_paths: List[Tuple[GitTreeResponseModel.GitTreeObject, str]]
    ) -> List[Document]:
        """
        Generate documents from a list of blobs and their full paths relative to the root of the repo.

        :param `blobs_and_paths`: list of tuples of (tree object, file's full path in the repo realtive to the root of the repo)
        :return: list of documents
        """
        documents: List[Document] = []
        for blob_object, full_path in blobs_and_paths:
            blob_data = self._client.get_blob(self._owner, self._repo, blob_object.sha)

            print_if_verbose(self._verbose, f"generating document for {full_path}")

            assert (
                blob_data.encoding == "base64"
            ), f"blob encoding {blob_data.encoding} not supported"

            decoded_bytes = None
            try:
                decoded_bytes = base64.b64decode(blob_data.content)
            except binascii.Error as e:
                print_if_verbose(self._verbose, f"error decoding {full_path}: {e}")
                continue

            if self._use_parser and (
                (
                    document := self._parse_supported_file(
                        file_path=full_path,
                        file_content=decoded_bytes,
                        tree_sha=blob_data.sha,
                        tree_path=full_path,
                    )
                )
                is not None
            ):
                documents.append(document)
                continue

            try:
                decoded_content_as_str = decoded_bytes.decode("utf-8")
            except UnicodeDecodeError as e:
                print_if_verbose(self._verbose, f"error decoding {full_path}: {e}")
                continue

            documents.append(
                Document(
                    text=decoded_content_as_str,
                    extra_info={
                        "full_path": full_path,
                        "file_name": os.path.basename(full_path),
                        "file_extension": os.path.splitext(full_path)[1],
                    },
                )
            )
        return documents

    def _parse_supported_file(
        self, file_path: str, file_content: bytes, tree_sha: str, tree_path: str
    ) -> Optional[Document]:
        """
        Parse a file if it is supported by a parser.

        :param `file_path`: path of the file in the repo
        :param `file_content`: content of the file
        :return: Document if the file is supported by a parser, None otherwise
        """
        file_extension = get_file_extension(file_path)
        if (parser := DEFAULT_FILE_EXTRACTOR.get(file_extension)) is not None:
            parser.init_parser()
            print_if_verbose(
                self._verbose,
                f"parsing {file_path} as {file_extension} with {parser.__class__.__name__}",
            )
            with tempfile.TemporaryDirectory() as tmpdirname:
                with tempfile.NamedTemporaryFile(
                    dir=tmpdirname,
                    suffix=f".{file_extension}",
                    mode="w+b",
                    delete=False,
                ) as tmpfile:
                    print_if_verbose(
                        self._verbose,
                        f"created a temporary file {tmpfile.name} for parsing {file_path}",
                    )
                    tmpfile.write(file_content)
                    tmpfile.flush()
                    tmpfile.close()
                    try:
                        parsed_file = parser.parse_file(pathlib.Path(tmpfile.name))
                        parsed_file = "\n\n".join(parsed_file)
                    except Exception as e:
                        print_if_verbose(
                            self._verbose, f"error while parsing {file_path}"
                        )
                        logger.error(
                            f"Error while parsing {file_path} with {parser.__class__.__name__}:\n{e}"
                        )
                        parsed_file = None
                    finally:
                        os.remove(tmpfile.name)
                    if parsed_file is None:
                        return None
                    return Document(
                        text=parsed_file,
                        doc_id=tree_sha,
                        extra_info={
                            "file_path": file_path,
                            "file_name": tree_path,
                        },
                    )
        return None


if __name__ == "__main__":
    import time

    def timeit(func: Callable) -> Callable:
        """Decorator to time a function."""

        def wrapper(*args: Any, **kwargs: Any) -> None:
            """Wrapper function."""
            start = time.time()
            func(*args, **kwargs)
            end = time.time()
            print(f"Time taken: {end - start} seconds for {func.__name__}")

        return wrapper

    reader1 = GithubRepositoryReader(
        github_token=os.environ["GITHUB_TOKEN"],
        owner="jerryjliu",
        repo="gpt_index",
        use_parser=False,
        verbose=True,
    )

    @timeit
    def load_data_from_commit() -> None:
        """Load data from a commit."""
        documents = reader1.load_data(
            commit_sha="22e198b3b166b5facd2843d6a62ac0db07894a13"
        )
        for document in documents:
            print(document.extra_info)

    @timeit
    def load_data_from_branch() -> None:
        """Load data from a branch."""
        documents = reader1.load_data(branch="main")
        for document in documents:
            print(document.extra_info)

    load_data_from_branch()
