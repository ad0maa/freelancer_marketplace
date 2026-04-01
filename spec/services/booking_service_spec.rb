require 'rails_helper'

RSpec.describe BookingService do
  describe '.create' do
    it 'creates a booking when the freelancer is available' do
      client = create(:client)
      freelancer = create(:freelancer, availability: true)

      result = described_class.create(
        client: client,
        freelancer: freelancer,
        start_date: Date.tomorrow,
        end_date: 1.week.from_now.to_date,
        total_amount: 500.00
      )

      expect(result).to be_success
      expect(result.booking).to be_persisted
      expect(result.booking.status).to eq('pending')
    end

    it 'fails when the freelancer is unavailable' do
      client = create(:client)
      freelancer = create(:freelancer, availability: false)

      result = described_class.create(
        client: client,
        freelancer: freelancer,
        start_date: Date.tomorrow,
        end_date: 1.week.from_now.to_date,
        total_amount: 500.00
      )

      expect(result).not_to be_success
      expect(result.error).to eq('Freelancer is not available')
    end

    it 'fails when dates overlap with an existing booking' do
      client = create(:client)
      freelancer = create(:freelancer)

      create(:booking,
             freelancer: freelancer,
             start_date: Date.tomorrow,
             end_date: 1.week.from_now.to_date,
             status: :confirmed)

      result = described_class.create(
        client: client,
        freelancer: freelancer,
        start_date: Date.tomorrow + 2.days,
        end_date: Date.tomorrow + 5.days,
        total_amount: 300.00
      )

      expect(result).not_to be_success
      expect(result.error).to eq('Freelancer has a conflicting booking')
    end

    it 'allows booking when existing booking is cancelled' do
      client = create(:client)
      freelancer = create(:freelancer)

      create(:booking,
             freelancer: freelancer,
             start_date: Date.tomorrow,
             end_date: 1.week.from_now.to_date,
             status: :cancelled)

      result = described_class.create(
        client: client,
        freelancer: freelancer,
        start_date: Date.tomorrow,
        end_date: 1.week.from_now.to_date,
        total_amount: 500.00
      )

      expect(result).to be_success
    end
  end
end
