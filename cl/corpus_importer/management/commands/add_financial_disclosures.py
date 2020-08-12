import argparse
import csv
import io
import re

import PyPDF2
import boto3
import requests
from PIL import Image
from botocore import UNSIGNED
from botocore.client import Config
from django.core.files.base import ContentFile

from cl.lib.command_utils import VerboseCommand, logger
from cl.people_db.models import FinancialDisclosure, Person

s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))
# We use the non-development bucket to test even though we eventually
# test save into development.  This is why we are setting these values instead
# of simply switching the defaults.
AWS_STORAGE_BUCKET_NAME = "com-courtlistener-storage"
AWS_S3_CUSTOM_DOMAIN = "%s.s3.amazonaws.com" % AWS_STORAGE_BUCKET_NAME


def make_key_path_dictionary(filepath):
    """Generate our lookup dictionary

    :param filepath: Filepath to processed CSV file
    :return: Object to lookup Judge PK from AWS URL
    :type return: dict
    """
    lookup_dict = {}
    with open(filepath) as tsvfile:
        reader = csv.reader(tsvfile)
        next(reader)
        for row in reader:
            lookup_dict[row[0].strip().replace("  ", " ")] = row[1].strip()
    return lookup_dict


def make_pdf_from_image_array(image_list):
    """Make a pdf given an array of Image files

    :param image_list: List of images
    :type image_list: list
    :return: pdf_data
    :type pdf_data: PDF as bytes
    """
    with io.BytesIO() as output:
        image_list[0].save(
            output,
            "PDF",
            resolution=100.0,
            save_all=True,
            append_images=image_list[1:],
        )
        pdf_data = output.getvalue()

    return pdf_data


def get_year_from_url(aws_url):
    """Extract year data from aws_url_path

    :param aws_url: URL of image file we want to process
    :type aws_url: str
    :return: Year extract from url
    :type return: str
    """
    year_regex = re.compile(r".*\/([0-9]{4})\/.*")
    m = year_regex.match(aws_url)
    year = m.group(1)
    return year


def query_thumbs_db(aws_url):
    """Query the indiviual image pages of a PDF based on the thumbs.db path.

    The function queries aws and sorts files that may not have leading zeroes
    correctly by page number.
    :param aws_url: URL of image file we want to process
    :type aws_url: str
    :return: Sorted urls for document & the first response key
    :type return: tuple
    """
    kwargs = {"Bucket": AWS_STORAGE_BUCKET_NAME, "Prefix": aws_url[:-10]}
    thumbs_db_query = s3.list_objects_v2(**kwargs)
    download_urls = [
        AWS_S3_CUSTOM_DOMAIN + x["Key"]
        for x in thumbs_db_query["Contents"]
        if "db" not in x["Key"]
    ]

    page_regex = re.compile(r"(.*Page_)(.*)(\.tif)")
    def key(item):
        m = page_regex.match(item)
        return int(m.group(2))

    download_urls.sort(key=key)

    return download_urls, thumbs_db_query["Contents"][0]["Key"]


def convert_long_image_to_pdf(aws_url):
    """Take a single image tiff and convert it into a multipage PDF.

    Download a single image and split it into its component pages.
    :param aws_url: URL of image file we want to process
    :type aws_url: str
    :return: An array of image data
    :type return: list
    """

    img = Image.open(
        requests.get("%s%s" % (base_url, aws_url_path), stream=True).raw
    )
    width, height = img.size
    image_list = []
    i, page_width, page_height = 0, width, (1046 * (float(width) / 792))
    while i < (height / page_height):
        image = img.crop(
            (0, (i * page_height), page_width, (i + 1) * page_height)
        )
        image_list.append(image)
        i += 1
    return image_list


def process_muti_image_financial_disclosures(options):
    """Find pre-split-tiffs and merge into a PDF

    The server is full of folders of images that comprise a single pdf.
    Identify split tiffs using the thumbail file and merge them together.

    :param options: The options provided at the command line.
    :return: None
    """
    aws_dict = make_key_path_dictionary(options["csv_path"])

    kwargs = {"Bucket": AWS_STORAGE_BUCKET_NAME, "Prefix": prefix}
    while True:
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp["Contents"]:
            aws_path = obj["Key"]
            if "Thumbs.db" not in aws_path:
                continue
            logger.info("Processing %s" % aws_path)

            sorted_urls, lookup = query_thumbs_db(aws_path)
            judge_pk = aws_dict[lookup]

            image_list = []
            for link in sorted_urls:
                image_list.append(
                    Image.open(requests.get(link, stream=True).raw)
                )
            pdf_content = make_pdf_from_image_array(image_list)

            fd = FinancialDisclosure(
                year=get_year_from_url(aws_path),
                page_count=len(image_list),
                person=Person.objects.get(id=judge_pk),
                person_id=judge_pk,
            )
            fd.filepath.save("", ContentFile(pdf_content))

        try:
            # Add the continuation token to continue iterating
            kwargs["ContinuationToken"] = resp["NextContinuationToken"]
        except KeyError:
            # If no continuation token we have reached the end and break
            break
    logger.info("No more PDFs to process.")


def process_single_image_financial_disclosures(options):
    """Find download and convert single image Tiffs into multi-page PDFs

    :param options: The options provided at the command line.
    :return:
    """
    aws_dict = make_key_path_dictionary(options["csv_path"])
    kwargs = {"Bucket": AWS_STORAGE_BUCKET_NAME, "Prefix": prefix}
    while True:
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp["Contents"]:
            aws_path = obj["Key"]
            if "Thumbs.db" in aws_path:
                continue
            if "Page" in aws_path:
                continue
            if ".pdf" in aws_path:
                continue

            judge_pk = aws_dict[aws_path]
            image_list = convert_long_image_to_pdf(aws_url_path=aws_path)
            pdf_content = make_pdf_from_image_array(image_list)

            fd = FinancialDisclosure(
                year=get_year_from_url(aws_path),
                page_count=len(image_list),
                person=Person.objects.get(id=judge_pk),
                person_id=judge_pk,
            )
            fd.filepath.save("", ContentFile(pdf_content))

        try:
            kwargs["ContinuationToken"] = resp["NextContinuationToken"]
        except KeyError:
            break
    logger.info("No more PDFs to process.")


def judicial_watch(options):
    """Find download and convert Judicial Watch PDFs.

    :param options: The options provided at the command line.
    :return: None
    """
    aws_dict = make_key_path_dictionary(options["csv_path"])
    kwargs = {"Bucket": AWS_STORAGE_BUCKET_NAME, "Prefix": jw_prefix}
    while True:
        logger.info("Querying Judicial Watch documents.")

        resp = s3.list_objects_v2(**kwargs)
        for obj in resp["Contents"]:
            aws_path = obj["Key"]
            lookup_key = aws_path.replace("  ", " ")
            judge_pk = aws_dict[lookup_key]

            pdf_content = requests.get(
                "http://%s%s" % (AWS_S3_CUSTOM_DOMAIN, aws_path)
            ).content

            with io.BytesIO(pdf_content) as open_pdf_file:
                pdf_data = PyPDF2.PdfFileReader(open_pdf_file)
                page_count = pdf_data.getNumPages()
            year = aws_path.split(" ")[-1][:4]

            fd = FinancialDisclosure(
                year=year,
                page_count=page_count,
                person=Person.objects.get(id=judge_pk),
                person_id=judge_pk,
            )
            fd.filepath.save("", ContentFile(pdf_content))
        try:
            kwargs["ContinuationToken"] = resp["NextContinuationToken"]
        except KeyError:
            break
    logger.info("No more PDFs to process.")

class Command(VerboseCommand):
    help = "Process and add Financial Disclousures on AWS into Courtlistener."

    def valid_actions(self, s):
        if s.lower() not in self.VALID_ACTIONS:
            raise argparse.ArgumentTypeError(
                "Unable to parse action. Valid actions are: %s"
                % (", ".join(self.VALID_ACTIONS.keys()))
            )

        return self.VALID_ACTIONS[s]

    def add_arguments(self, parser):
        parser.add_argument(
            "--action",
            type=self.valid_actions,
            required=True,
            help="The action you wish to take. Valid choices are: %s"
            % (", ".join(self.VALID_ACTIONS.keys())),
        )

        parser.add_argument(
            "--csv-path",
            default="cl/corpus_importer/tmp/target_pkupdated.csv",
            required=False,
            help="Path to our pre-generated csv file",
        )

    def handle(self, *args, **options):
        super(Command, self).handle(*args, **options)
        options["action"](options)

    VALID_ACTIONS = {
        "multiple-image-fd": process_muti_image_financial_disclosures,
        "single-image-fd": process_single_image_financial_disclosures,
        "judicial-watch": judicial_watch,
    }
